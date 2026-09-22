"""Map Slack/Feishu-like webhook text to the shared AgentService."""

from __future__ import annotations

from hashlib import sha256
from typing import Any

from queryforge.application import AgentOptions, AgentService

#: Run statuses that mean *no answer was produced*. A governance block, a failure,
#: a cancellation and a pending clarification are all terminal or blocking
#: outcomes, so reporting them as "Query completed. Returned 0 row(s)." told the
#: chat user the exact opposite of what happened (C1). Any other status
#: (``success``, ``planned``, ...) keeps the original completed wording.
_UNANSWERED_STATUSES = frozenset(
    {"blocked", "failed", "cancelled", "needs_clarification"}
)

#: How each unanswered status is announced to the channel. The text leads with
#: the outcome so a reader never mistakes it for a successful answer.
_STATUS_PREFIX = {
    "blocked": "Query blocked",
    "failed": "Query failed",
    "cancelled": "Query cancelled",
    "needs_clarification": "Clarification needed",
}

#: Fallback sentence per status, used when the run carried no explanation at all.
_STATUS_FALLBACK = {
    "blocked": "governance stopped the run before it produced an answer",
    "failed": "the run failed before it produced an answer",
    "cancelled": "the run was cancelled before it produced an answer",
    "needs_clarification": "the question is ambiguous and needs more detail",
}


class GatewayAdapter:
    def __init__(self, service: AgentService | None = None, preview_rows: int = 5) -> None:
        self.service = service or AgentService()
        self.preview_rows = preview_rows

    def handle(self, *, user_id: str, channel: str, text: str) -> dict:
        if not user_id.strip() or not channel.strip() or not text.strip():
            raise ValueError("user_id, channel, and text must be non-empty")
        session_id = self._session_id(user_id, channel)
        output = self.service.ask(
            text,
            AgentOptions(entrypoint="gateway", session_id=session_id),
        )
        status = str(output.get("status") or "success")
        row_count = int(output.get("row_count") or 0)
        payload = {
            "run_id": output.get("run_id"),
            "user_id": user_id,
            "channel": channel,
            # ``status`` is part of the payload so a channel integration can
            # branch on it instead of pattern-matching the human sentence.
            "status": status,
            "text": self._text(output, status=status, row_count=row_count),
            "sql": output.get("sql"),
            "columns": output.get("columns", []),
            "rows_preview": output.get("rows", [])[: self.preview_rows],
            "row_count": row_count,
            "session_id": output.get("session", {}).get("session_id", session_id),
        }
        if status in _UNANSWERED_STATUSES:
            payload["reason"] = self._reason(output)
        return payload

    # ------------------------------------------------------------------ wording

    def _text(self, output: dict, *, status: str, row_count: int) -> str:
        """The human sentence a channel posts for one run outcome.

        An unanswered run never claims a completion, and it never hides the
        reason: the governance/policy text (or, for a clarification, the question
        that has to be answered) is what makes the message actionable.
        """

        if status not in _UNANSWERED_STATUSES:
            explanation = str(output.get("explanation") or "Query completed.")
            return f"{explanation} Returned {row_count} row(s)."
        if status == "needs_clarification":
            questions = self._clarification_questions(output)
            detail = "; ".join(questions) or self._reason(output)
        else:
            detail = self._reason(output)
        return f"{_STATUS_PREFIX[status]}: {detail}"

    @staticmethod
    def _reason(output: dict) -> str:
        """The most specific explanation a run carried, one bounded sentence.

        Every candidate is a short, already user-facing string (the workflow and
        the planner write these fields for operators); the run's full context dump
        is never used, so a webhook reply cannot leak agent state. The run-level
        text (the error that stopped it) is preferred over the phase-level
        ``agent_team.blocked_reason``, which is the fallback.
        """

        agent_team = output.get("agent_team")
        team = agent_team if isinstance(agent_team, dict) else {}
        for candidate in (
            output.get("reason"),
            output.get("blocked_reason"),
            output.get("error"),
            output.get("detail"),
            output.get("message"),
            team.get("blocked_reason"),
        ):
            if isinstance(candidate, str) and candidate.strip():
                return candidate.strip()
        return _STATUS_FALLBACK.get(
            str(output.get("status") or ""), "the run produced no answer"
        )

    @classmethod
    def _clarification_questions(cls, output: dict) -> list[str]:
        """Collect the questions a ``needs_clarification`` run asks the user.

        The workflow records them under several names depending on the stage
        (``unresolved_questions``, ``clarifications`` from the analysis artifact,
        ``needs_clarification`` on the session); all of them are accepted so the
        webhook reply carries the actual question instead of a generic notice.
        """

        session = output.get("session")
        candidates: list[Any] = [
            output.get("unresolved_questions"),
            output.get("clarifications"),
            output.get("needs_clarification"),
            (session or {}).get("needs_clarification")
            if isinstance(session, dict)
            else None,
        ]
        questions: list[str] = []
        for candidate in candidates:
            items = candidate if isinstance(candidate, (list, tuple)) else [candidate]
            for item in items:
                text = cls._question_text(item)
                if text and text not in questions:
                    questions.append(text)
        return questions

    @staticmethod
    def _question_text(item: Any) -> str | None:
        if isinstance(item, str):
            return item.strip() or None
        if isinstance(item, dict):
            for key in ("question", "reason", "aspect", "detail", "message"):
                value = item.get(key)
                if isinstance(value, str) and value.strip():
                    return value.strip()
        return None

    @staticmethod
    def _session_id(user_id: str, channel: str) -> str:
        digest = sha256(
            f"{user_id.strip()}\0{channel.strip()}".encode("utf-8")
        ).hexdigest()
        return f"gateway_{digest}"
