"""Full FastMCP mapping over QueryForge's shared service layer."""

from __future__ import annotations

import argparse
from dataclasses import dataclass

from queryforge.interfaces.mcp import prompts
from queryforge.application import AgentOptions, AgentService


class MCPUnavailableError(RuntimeError):
    pass


@dataclass
class _SessionState:
    session_id: str | None = None


def _features(service: AgentService):
    loader = getattr(service, "config_loader", None)
    if loader is None:
        return {
            "resources": True,
            "prompts": True,
            "sessions": True,
            "history_limit": 20,
        }
    config = loader()
    return {
        "resources": config.mcp_resources_enabled,
        "prompts": config.mcp_prompts_enabled,
        "sessions": config.mcp_session_enabled,
        "history_limit": config.mcp_history_limit,
    }


def create_mcp_server(service: AgentService | None = None):
    try:
        from mcp.server.fastmcp import FastMCP
    except ImportError as exc:
        raise MCPUnavailableError(
            "MCP server is unavailable because the optional 'mcp' SDK is not "
            "installed. Install it with: pip install -r requirements-mcp.txt"
        ) from exc

    agent_service = service or AgentService()
    mcp = FastMCP("QueryForge", json_response=True)
    features = _features(agent_service)
    session_state = _SessionState()

    def resolved_session(session_id: str | None) -> str | None:
        if not features["sessions"]:
            return session_id
        if session_id:
            session_state.session_id = session_id
            return session_id
        if session_state.session_id is None:
            session_state.session_id = agent_service.new_session()["session_id"]
        return session_state.session_id

    @mcp.tool()
    def ask_sql(
        question: str,
        database: str | None = None,
        semantic_model_path: str | None = None,
        allow_schema_only: bool = False,
        subject_tree_enabled: bool = False,
        subject_tree_path: str | None = None,
        subject: str | None = None,
        default_subject: str | None = None,
        sql_policy_path: str | None = None,
        model_provider: str | None = None,
        model: str | None = None,
        skills: list[str] | None = None,
        visualize: bool = False,
        report: bool = False,
        session_id: str | None = None,
        reset_session: bool = False,
        tool_loop_enabled: bool = False,
        tool_loop_max_rounds: int = 5,
        tool_loop_timeout_seconds: float = 30,
        tool_loop_preview_limit: int = 20,
        parallel_candidates: int = 1,
        parallel_max_preview: int = 2,
        parallel_preview_limit: int = 20,
        parallel_preview_timeout_seconds: float = 10,
        complexity_mode: str = "auto",
    ) -> dict:
        """Generate and execute one read-only SQLite query."""
        return agent_service.ask(
            question,
            AgentOptions(
                database=database,
                semantic_model_path=semantic_model_path,
                allow_schema_only=allow_schema_only,
                subject_tree_enabled=subject_tree_enabled,
                subject_tree_path=subject_tree_path,
                subject=subject,
                default_subject=default_subject,
                sql_policy_path=sql_policy_path,
                model_provider=model_provider,
                model=model,
                skills=skills,
                visualize=visualize,
                session_id=resolved_session(session_id),
                reset_session=reset_session,
                tool_loop_enabled=tool_loop_enabled,
                tool_loop_max_rounds=tool_loop_max_rounds,
                tool_loop_timeout_seconds=tool_loop_timeout_seconds,
                tool_loop_preview_limit=tool_loop_preview_limit,
                parallel_candidates=parallel_candidates,
                parallel_max_preview=parallel_max_preview,
                parallel_preview_limit=parallel_preview_limit,
                parallel_preview_timeout_seconds=parallel_preview_timeout_seconds,
                complexity_mode=complexity_mode,
                report=report,
                entrypoint="mcp",
            ),
        )

    @mcp.tool()
    def list_models() -> list[dict]:
        """List QueryForge model providers without exposing API keys."""
        return agent_service.list_models()

    @mcp.tool()
    def list_skills() -> list[dict]:
        """List local prompt-only QueryForge Skills."""
        return agent_service.list_skills()

    @mcp.tool()
    def list_subjects(subject_tree_path: str | None = None) -> list[dict]:
        """List configured declarative analytical subjects."""
        return agent_service.list_subjects(subject_tree_path)

    @mcp.tool()
    def get_history(limit: int = 20) -> dict:
        """Return recent compact SQL history; result rows are never stored."""
        return agent_service.get_history(min(limit, features["history_limit"]))

    @mcp.tool()
    def list_tables(
        database: str | None = None,
        sql_policy_path: str | None = None,
    ) -> list[dict]:
        """List policy-authorized SQLite tables."""
        return agent_service.list_tables(database, sql_policy_path)

    @mcp.tool()
    def describe_table(
        table_name: str,
        database: str | None = None,
        sql_policy_path: str | None = None,
        sample_limit: int = 5,
    ) -> dict:
        """Return policy-filtered schema and bounded sample rows."""
        return agent_service.describe_table(
            table_name, database, sql_policy_path, sample_limit
        )

    @mcp.tool()
    def list_metrics(
        database: str | None = None,
        semantic_model_path: str | None = None,
    ) -> list[dict]:
        """List metrics from the validated semantic model."""
        return agent_service.list_metrics(database, semantic_model_path)

    @mcp.tool()
    def preview_sql(
        sql: str,
        database: str | None = None,
        sql_policy_path: str | None = None,
        limit: int = 20,
    ) -> dict:
        """Preview a bounded read-only SQL result through the shared AST policy."""
        return agent_service.preview_sql(sql, database, sql_policy_path, limit)

    @mcp.tool()
    def review_sql(
        sql: str,
        database: str | None = None,
        sql_policy_path: str | None = None,
    ) -> dict:
        """Review SQL through the shared SQL-review pipeline."""
        return agent_service.ask(
            f"Review SQL: {sql}",
            AgentOptions(
                database=database,
                sql_policy_path=sql_policy_path,
                provided_sql=sql,
                skills=[],
                entrypoint="mcp",
            ),
        )

    @mcp.tool()
    def new_session(session_id: str | None = None) -> dict:
        """Create and select a new Conversation Memory session."""
        if not features["sessions"]:
            raise ValueError("MCP session support is disabled")
        session = agent_service.new_session(session_id=session_id)
        session_state.session_id = session["session_id"]
        return session

    @mcp.tool()
    def reset_session(session_id: str | None = None) -> dict:
        """Reset the selected or explicitly supplied Conversation Memory session."""
        if not features["sessions"]:
            raise ValueError("MCP session support is disabled")
        resolved = resolved_session(session_id)
        assert resolved is not None
        return agent_service.reset_session(resolved)

    if features["resources"]:
        @mcp.resource("queryforge://tables")
        def tables_resource() -> list[dict]:
            return agent_service.list_tables()

        @mcp.resource("queryforge://tables/{table_name}")
        def table_resource(table_name: str) -> dict:
            return agent_service.describe_table(table_name)

        @mcp.resource("queryforge://metrics")
        def metrics_resource() -> list[dict]:
            try:
                return agent_service.list_metrics()
            except ValueError:
                return []

        @mcp.resource("queryforge://metrics/{metric_name}")
        def metric_resource(metric_name: str) -> dict:
            return agent_service.get_metric(metric_name)

        @mcp.resource("queryforge://history")
        def history_resource() -> dict:
            return agent_service.get_history(features["history_limit"])

        @mcp.resource("queryforge://skills")
        def skills_resource() -> list[dict]:
            return agent_service.list_skills()

        @mcp.resource("queryforge://subjects")
        def subjects_resource() -> list[dict]:
            try:
                return agent_service.list_subjects()
            except ValueError:
                return []

    if features["prompts"]:
        @mcp.prompt(name="queryforge.analyze_data")
        def queryforge_analyze_data(question: str, subject: str | None = None) -> str:
            return prompts.analyze_data(question, subject)

        @mcp.prompt(name="queryforge.sql_review")
        def queryforge_sql_review(sql: str) -> str:
            return prompts.sql_review(sql)

        @mcp.prompt(name="queryforge.troubleshoot")
        def queryforge_troubleshoot(sql: str, error_message: str) -> str:
            return prompts.troubleshoot(sql, error_message)

        @mcp.prompt(name="queryforge.build_report")
        def queryforge_build_report(
            question: str,
            subject: str | None = None,
            metrics: str | None = None,
        ) -> str:
            return prompts.build_report(question, subject, metrics)

    return mcp


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the QueryForge MCP server")
    parser.add_argument(
        "--transport",
        choices=("stdio", "streamable-http"),
        default="stdio",
    )
    args = parser.parse_args()
    try:
        server = create_mcp_server()
    except MCPUnavailableError as exc:
        print(f"QueryForge MCP unavailable: {exc}")
        return 1
    server.run(transport=args.transport)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
