"""Map Slack/Feishu-like webhook text to the shared AgentService."""

from __future__ import annotations

from hashlib import sha256

from queryforge.application import AgentOptions, AgentService


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
        row_count = int(output.get("row_count") or 0)
        explanation = str(output.get("explanation") or "Query completed.")
        return {
            "run_id": output.get("run_id"),
            "user_id": user_id,
            "channel": channel,
            "text": f"{explanation} Returned {row_count} row(s).",
            "sql": output.get("sql"),
            "columns": output.get("columns", []),
            "rows_preview": output.get("rows", [])[: self.preview_rows],
            "row_count": row_count,
            "session_id": output.get("session", {}).get("session_id", session_id),
        }

    @staticmethod
    def _session_id(user_id: str, channel: str) -> str:
        digest = sha256(
            f"{user_id.strip()}\0{channel.strip()}".encode("utf-8")
        ).hexdigest()
        return f"gateway_{digest}"
