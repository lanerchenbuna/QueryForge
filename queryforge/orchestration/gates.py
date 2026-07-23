"""Deterministic artifact quality gates for orchestration stages."""

from __future__ import annotations

import json
from typing import Any

from queryforge.core.schemas.models import Context
from queryforge.orchestration.quality import append_warning
from queryforge.orchestration.runtime.state_store import AgentTeamStateStore
from queryforge.orchestration.schemas import TaskState


class QualityGateEvaluator:
    """Read artifacts, aggregate warnings, and block invalid stage transitions."""

    _ARTIFACT_TYPES = {
        "analysis": {"analysis_request", "knowledge_context", "schema_plan"},
        "schema": {"schema_plan"},
        "sql_candidate": {"sql_candidate", "governance_report"},
        "execution": set(),
        "qa": {"qa_report"},
        "visualization": {"visualization_artifact"},
        "review": {"review_report"},
        "ops": {"ops_report"},
    }

    def __init__(self, state_store: AgentTeamStateStore) -> None:
        self.state_store = state_store

    def evaluate(
        self,
        state: TaskState,
        phase: str,
        context: Context | None = None,
    ) -> bool:
        relevant = [
            artifact
            for artifact in self.documents(state)
            if artifact.get("artifact_type")
            in self._ARTIFACT_TYPES.get(phase, set())
        ]
        self._append_warnings(state, phase, relevant)
        blocked_reasons = [
            self._block_reason(artifact)
            for artifact in relevant
            if artifact.get("status") == "blocked"
        ]
        blocked_reasons.extend(
            self._deterministic_failures(phase, relevant, context)
        )
        reasons = [reason for reason in blocked_reasons if reason]
        if not reasons:
            return False
        self.block(
            state,
            phase=phase,
            reason="; ".join(dict.fromkeys(reasons)),
            context=context,
        )
        return True

    def block(
        self,
        state: TaskState,
        *,
        phase: str,
        reason: str,
        context: Context | None,
    ) -> None:
        state.status = "blocked"
        state.current_phase = "blocked"
        state.blocked_phase = phase
        state.blocked_reason = reason
        if context is None:
            return
        blocked_artifacts = [
            artifact
            for artifact in self.documents(state)
            if artifact.get("status") == "blocked"
        ]
        context.final_output = {
            "status": "blocked",
            "run_id": state.run_id,
            "question": context.task.question,
            "phase": phase,
            "reason": reason,
            "clarification": self._clarifications(blocked_artifacts),
        }

    def documents(self, state: TaskState) -> list[dict[str, Any]]:
        documents: list[dict[str, Any]] = []
        run_dir = self.state_store.run_dir(state.run_id)
        for artifact in state.artifacts:
            try:
                documents.append(
                    json.loads((run_dir / artifact.path).read_text(encoding="utf-8"))
                )
            except (OSError, UnicodeError, json.JSONDecodeError):
                continue
        return documents

    def _append_warnings(
        self,
        state: TaskState,
        phase: str,
        artifacts: list[dict[str, Any]],
    ) -> None:
        for artifact in artifacts:
            if artifact.get("status") not in {"warning", "degraded"}:
                continue
            append_warning(
                state,
                phase=phase,
                artifact_type=str(artifact.get("artifact_type")),
                producer=str(artifact.get("producer")),
                reason=self._warning_reason(artifact),
            )

    @staticmethod
    def _deterministic_failures(
        phase: str,
        artifacts: list[dict[str, Any]],
        context: Context | None,
    ) -> list[str]:
        failures: list[str] = []
        by_type = {
            artifact.get("artifact_type"): artifact for artifact in artifacts
        }
        if phase == "analysis":
            analysis = by_type.get("analysis_request")
            if analysis:
                payload = analysis.get("payload") or {}
                if not (
                    payload.get("goal")
                    or payload.get("objective")
                    or payload.get("question")
                ):
                    failures.append("analysis_request has no identifiable goal")
            schema = by_type.get("schema_plan")
            if schema and not _has_selected_tables(schema.get("payload") or {}):
                failures.append("schema_plan did not find any relevant table")
        elif phase == "schema":
            schema = by_type.get("schema_plan")
            if not schema or not _has_selected_tables(schema.get("payload") or {}):
                failures.append("schema_plan did not find any relevant table")
        elif phase == "sql_candidate":
            candidate = by_type.get("sql_candidate")
            if candidate and not (candidate.get("payload") or {}).get("sql"):
                failures.append("sql_candidate has no SQL")
            governance = by_type.get("governance_report")
            payload = governance.get("payload") if governance else {}
            if payload and payload.get("allowed") is False:
                failures.append(payload.get("reason") or "governance rejected SQL")
        elif phase == "execution" and context is not None:
            if context.execution_result is None and context.last_execution_error:
                failures.append(context.last_execution_error)
        elif phase == "qa":
            qa = by_type.get("qa_report")
            payload = qa.get("payload") if qa else {}
            severe = [
                issue
                for issue in (payload or {}).get("issues", [])
                if issue.get("severity") == "error"
            ]
            if payload and payload.get("passed") is False and severe:
                failures.append("qa_report found severe data quality issue")
        return failures

    @staticmethod
    def _warning_reason(artifact: dict[str, Any]) -> str:
        payload = artifact.get("payload") or {}
        if payload.get("artifact_schema_error"):
            return "Artifact schema validation degraded this payload."
        if payload.get("retrieval_status", {}).get("history_warning"):
            return str(payload["retrieval_status"]["history_warning"])
        if payload.get("clarification_reasons"):
            return "; ".join(payload["clarification_reasons"])
        if payload.get("risks"):
            return "; ".join(
                risk.get("description", "") for risk in payload["risks"]
            )
        if payload.get("findings"):
            return "; ".join(
                finding.get("reason", "") for finding in payload["findings"]
            )
        return f"{artifact.get('artifact_type')} reported {artifact.get('status')}"

    @staticmethod
    def _block_reason(artifact: dict[str, Any]) -> str:
        payload = artifact.get("payload") or {}
        if payload.get("reason"):
            return str(payload["reason"])
        if payload.get("clarification_reasons"):
            return "; ".join(payload["clarification_reasons"])
        if payload.get("findings"):
            return "; ".join(
                finding.get("reason", "")
                for finding in payload["findings"]
                if finding.get("severity") == "error"
            )
        if payload.get("risks"):
            return "; ".join(
                risk.get("description", "") for risk in payload["risks"]
            )
        return f"{artifact.get('artifact_type')} blocked the workflow"

    @staticmethod
    def _clarifications(artifacts: list[dict[str, Any]]) -> list[dict]:
        return [
            clarification
            for artifact in artifacts
            for clarification in (artifact.get("payload") or {}).get(
                "clarifications", []
            )
        ]


def _has_selected_tables(payload: dict[str, Any]) -> bool:
    return bool(payload.get("tables") or payload.get("selected_tables"))
