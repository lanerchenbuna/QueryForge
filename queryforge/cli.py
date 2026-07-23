"""Canonical command-line entry point for QueryForge."""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

from queryforge.workflow.workflow_runner import WorkflowRunner
from queryforge.core.config import load_config
from queryforge.infrastructure.db.sqlite_connector import SQLiteConnector
from queryforge.core.schemas.models import ExecutionPlan
from queryforge.core.observability import configure_logging, new_run_id
from queryforge.application import AgentOptions, AgentService
from queryforge.infrastructure.storage import (
    KnowledgeBaseBuilder,
    LanceDBVectorStore,
    OpenAIEmbeddingProvider,
    SQLHistoryStore,
)
from queryforge.infrastructure.tools.database_tool import DatabaseTool
from queryforge.domain.semantic.builder import SemanticBuildError, SemanticModelBuilder


LOGGER = logging.getLogger("queryforge.cli")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run the QueryForge natural-language-to-SQL workflow."
    )
    parser.add_argument("--question", help="Question to ask")
    parser.add_argument(
        "--log-level",
        help="Console/file log level; overrides LOG_LEVEL (DEBUG/INFO/WARNING/ERROR/CRITICAL)",
    )
    parser.add_argument(
        "--debug-prompts",
        action="store_true",
        help="Explicitly save full model prompt/response traces under .queryforge/traces",
    )
    parser.add_argument(
        "--show-run-summary",
        action="store_true",
        help="Include the structured run summary in the final JSON",
    )
    parser.add_argument(
        "--report",
        action="store_true",
        help="Generate a static HTML analysis report after successful execution",
    )
    parser.add_argument(
        "--report-output-dir",
        help="Directory for generated HTML reports",
    )
    parser.add_argument(
        "--stream",
        action="store_true",
        help="Show progress events on stderr while keeping final JSON on stdout",
    )
    parser.add_argument(
        "--session-id",
        help="Persistent conversation session ID for follow-up questions",
    )
    parser.add_argument(
        "--new-session",
        action="store_true",
        help="Create a new conversation session and ignore any supplied session ID",
    )
    parser.add_argument(
        "--reset-session",
        action="store_true",
        help="Clear the supplied conversation session before this question",
    )
    parser.add_argument(
        "--tool-loop",
        action="store_true",
        help="Enable bounded read-only schema/data observation before SQL generation",
    )
    parser.add_argument("--tool-loop-max-rounds", type=int, default=5)
    parser.add_argument("--tool-loop-timeout", type=float, default=30)
    parser.add_argument("--tool-loop-preview-limit", type=int, default=20)
    parser.add_argument(
        "--parallel-candidates",
        type=int,
        default=1,
        help="Generate 1-3 SQL candidates and select the best preview (default: 1)",
    )
    parser.add_argument("--parallel-max-preview", type=int, default=2)
    parser.add_argument("--parallel-preview-limit", type=int, default=20)
    parser.add_argument("--parallel-preview-timeout", type=float, default=10)
    parser.add_argument(
        "--complexity-mode",
        choices=("auto", "simple", "complex"),
        default="auto",
        help="Choose automatic, simple, or complex execution profile",
    )
    parser.add_argument(
        "--serve-api",
        action="store_true",
        help="Start the optional FastAPI server instead of running one query",
    )
    parser.add_argument("--api-host", default="127.0.0.1")
    parser.add_argument("--api-port", type=int, default=8000)
    parser.add_argument(
        "--serve-mcp",
        action="store_true",
        help="Start the optional MCP server instead of running one query",
    )
    parser.add_argument(
        "--mcp-transport",
        choices=("stdio", "streamable-http"),
        default="stdio",
    )
    parser.add_argument(
        "--database",
        help="Path to a SQLite database (defaults to DATABASE_PATH)",
    )
    parser.add_argument(
        "--semantic-model",
        help=(
            "YAML semantic model validated against the selected SQLite database; "
            "a conventional sibling model is discovered automatically"
        ),
    )
    parser.add_argument(
        "--allow-schema-only",
        action="store_true",
        help=(
            "Explicit diagnostic escape hatch that permits a query without a "
            "semantic model"
        ),
    )
    parser.add_argument(
        "--build-semantic-model",
        action="store_true",
        help=(
            "Infer, merge, validate, and publish a semantic model for --database, "
            "then exit"
        ),
    )
    parser.add_argument(
        "--semantic-output",
        help=(
            "Output for --build-semantic-model (default: semantic_model.yml "
            "beside the database)"
        ),
    )
    parser.add_argument(
        "--semantic-base",
        help="Optional curated model merged by --build-semantic-model",
    )
    parser.add_argument("--semantic-name", help="Name for a built semantic model")
    parser.add_argument(
        "--semantic-owner",
        default="data-platform",
        help="Default owner for inferred semantic objects",
    )
    parser.add_argument(
        "--replace-semantic-model",
        action="store_true",
        help="Build a fresh model instead of merging an existing output",
    )
    parser.add_argument(
        "--draft-semantic-model",
        action="store_true",
        help="Validate and retain a draft without publishing the semantic model",
    )
    parser.add_argument(
        "--subject-tree",
        help="Optional YAML subject tree used to bound schema and semantic context",
    )
    parser.add_argument(
        "--enable-subject-tree",
        action="store_true",
        help="Enable subject-tree scoping for this request",
    )
    parser.add_argument(
        "--subject",
        help="Explicit subject ID from the enabled subject tree",
    )
    parser.add_argument(
        "--default-subject",
        help="Override the subject tree's configured fallback subject",
    )
    parser.add_argument(
        "--list-subjects",
        action="store_true",
        help="List subjects from --subject-tree or SUBJECT_TREE_PATH, then exit",
    )
    parser.add_argument(
        "--sql-policy",
        help=(
            "Optional YAML SQL security policy for table/column scope, functions, "
            "and LIMIT enforcement"
        ),
    )
    parser.add_argument(
        "--model-provider",
        help="Override the active model provider from models.yml or LLM_PROVIDER",
    )
    parser.add_argument(
        "--model",
        help="Override the selected provider's model name",
    )
    parser.add_argument(
        "--list-models",
        action="store_true",
        help="List configured providers and models, then exit",
    )
    parser.add_argument(
        "--list-skills",
        action="store_true",
        help="List local prompt skills, then exit",
    )
    parser.add_argument(
        "--skills",
        help=(
            "Skill mode: auto (default), none, or comma-separated names for "
            "manual loading"
        ),
    )
    parser.add_argument(
        "--date-llm-fallback",
        action="store_true",
        help="Use the selected LLM when date rules find no expression",
    )
    parser.add_argument(
        "--plan-mode",
        action="store_true",
        help="Show the generated SQL plan and require approval before execution",
    )
    parser.add_argument(
        "--auto-approve-plan",
        action="store_true",
        help="Approve plan mode without interactive input (tests/API automation)",
    )
    parser.add_argument(
        "--max-retries",
        type=int,
        default=2,
        help="Maximum SQL fix/regeneration retries (default: 2)",
    )
    parser.add_argument(
        "--history-top-k",
        type=int,
        default=3,
        help="Number of similar successful SQL history entries to inject (default: 3)",
    )
    parser.add_argument(
        "--visualize",
        action="store_true",
        help="Generate a rule-selected Vega-Lite chart configuration",
    )
    parser.add_argument(
        "--chart-output-dir",
        default=".queryforge/charts",
        help="Directory for generated .vl.json chart files (default: .queryforge/charts)",
    )
    parser.add_argument(
        "--enable-vector-kb",
        action="store_true",
        help="Enable optional LanceDB vector retrieval for this query",
    )
    parser.add_argument(
        "--rebuild-vector-kb",
        action="store_true",
        help="Rebuild LanceDB from SQL history, current schema, and --kb-source paths",
    )
    parser.add_argument(
        "--vector-top-k",
        type=int,
        default=3,
        help="Maximum matches for each vector context category (default: 3)",
    )
    parser.add_argument(
        "--kb-stats",
        action="store_true",
        help="Show LanceDB vector table counts and exit",
    )
    parser.add_argument(
        "--kb-source",
        action="append",
        default=[],
        metavar="PATH",
        help="SQL/Jinja/CSV file or directory included during rebuild; repeatable",
    )
    parser.add_argument(
        "--show-history",
        action="store_true",
        help="Show recent persisted SQL history and exit",
    )
    parser.add_argument(
        "--history-limit",
        type=int,
        default=50,
        help="Maximum entries shown by --show-history (default: 50)",
    )
    parser.add_argument(
        "--import-success-stories",
        metavar="CSV_PATH",
        help="Import question/SQL examples from a success_story CSV",
    )
    parser.add_argument(
        "--import-reference-sql",
        metavar="PATH",
        help="Import one SQL file or a directory of reference SQL files",
    )
    parser.add_argument(
        "--clear-history",
        action="store_true",
        help="Clear persisted SQL history (requires --confirm-clear-history)",
    )
    parser.add_argument(
        "--confirm-clear-history",
        action="store_true",
        help="Explicitly confirm --clear-history",
    )
    parser.add_argument(
        "--prepare-sample-data",
        action="store_true",
        help="Check that the bundled synthetic anime streaming sample is complete",
    )
    parser.add_argument(
        "--show-workflow",
        action="store_true",
        help="Print the fixed workflow before running it",
    )
    return parser


def main() -> int:
    args = build_parser().parse_args()
    run_id = new_run_id()
    try:
        log_path = configure_logging(args.log_level)
    except ValueError as exc:
        print(f"QueryForge failed: {exc}", file=sys.stderr)
        return 2
    LOGGER.info(
        "cli_start log_path=%s debug_prompts=%s show_run_summary=%s",
        log_path,
        args.debug_prompts,
        args.show_run_summary,
        extra={"run_id": run_id},
    )
    if args.serve_api and args.serve_mcp:
        print("QueryForge failed: choose only one of --serve-api or --serve-mcp", file=sys.stderr)
        return 2
    if args.serve_api:
        try:
            import uvicorn
        except ImportError:
            print(
                "QueryForge API unavailable: install with "
                "pip install -r requirements-server.txt",
                file=sys.stderr,
            )
            return 1
        uvicorn.run(
            "queryforge.interfaces.api.app:create_app",
            factory=True,
            host=args.api_host,
            port=args.api_port,
        )
        return 0
    if args.serve_mcp:
        try:
            from queryforge.interfaces.mcp.server import create_mcp_server

            mcp_server = create_mcp_server()
        except Exception as exc:
            print(f"QueryForge MCP unavailable: {exc}", file=sys.stderr)
            return 1
        mcp_server.run(transport=args.mcp_transport)
        return 0

    service = AgentService(config_loader=load_config)

    if args.prepare_sample_data:
        try:
            from sample.prepare_sample_data import main as prepare_sample_data
        except ImportError:
            print(
                "QueryForge sample utilities are available from a source checkout. "
                "Clone the repository or provide your own --database and semantic model.",
                file=sys.stderr,
            )
            return 1
        return prepare_sample_data()
    if args.build_semantic_model:
        try:
            config = load_config(
                provider_override=args.model_provider,
                model_override=args.model,
            )
            database = Path(args.database or config.database_path).expanduser().resolve()
            output = (
                Path(args.semantic_output).expanduser().resolve()
                if args.semantic_output
                else database.parent / "semantic_model.yml"
            )
            existing = args.semantic_base
            if (
                not args.replace_semantic_model
                and not existing
                and output.is_file()
            ):
                existing = str(output)
            result = SemanticModelBuilder(
                database,
                owner=args.semantic_owner,
            ).build(
                output,
                existing_model_path=existing,
                name=args.semantic_name,
                publish=not args.draft_semantic_model,
            )
        except (SemanticBuildError, ValueError, OSError) as exc:
            print(f"QueryForge semantic build failed: {exc}", file=sys.stderr)
            return 1
        print(json.dumps(result.summary(), ensure_ascii=False, indent=2))
        return 0 if result.contract_passed else 1

    if args.show_workflow:
        print(WorkflowRunner.describe_workflow())
        return 0

    if args.list_skills:
        try:
            skills = service.list_skills()
        except Exception as exc:
            print(f"QueryForge failed: invalid skills configuration: {exc}", file=sys.stderr)
            return 1
        for skill in skills:
            marker = "*" if skill["enabled"] else " "
            print(
                f"{marker} {skill['name']:<32} priority={skill['priority']:<3} "
                f"nodes={','.join(skill['allowed_nodes'])}\n"
                f"    {skill['description']}"
            )
        return 0
    if args.list_subjects:
        try:
            print(
                json.dumps(
                    service.list_subjects(args.subject_tree),
                    ensure_ascii=False,
                    indent=2,
                )
            )
        except Exception as exc:
            print(f"QueryForge failed: {exc}", file=sys.stderr)
            return 1
        return 0

    if args.list_models:
        try:
            models = service.list_models(args.model_provider, args.model)
        except Exception as exc:
            print(f"QueryForge failed: invalid model configuration: {exc}", file=sys.stderr)
            return 1
        for definition in models:
            marker = "*" if definition["active"] else " "
            print(
                f"{marker} {definition['name']:<10} "
                f"type={definition['type']:<18} model={definition['model']}"
            )
        return 0

    vector_action = args.rebuild_vector_kb or args.kb_stats
    if args.kb_source and not args.rebuild_vector_kb:
        print(
            "QueryForge failed: --kb-source requires --rebuild-vector-kb",
            file=sys.stderr,
        )
        return 2
    if vector_action:
        if args.vector_top_k < 0 or args.vector_top_k > 20:
            print(
                "QueryForge failed: --vector-top-k must be between 0 and 20",
                file=sys.stderr,
            )
            return 2
        try:
            kb_config = load_config(
                provider_override=args.model_provider,
                model_override=args.model,
            )
            vector_store = LanceDBVectorStore(
                kb_config.vector_kb_path,
                embedding_provider=OpenAIEmbeddingProvider(
                    kb_config.embedding_api_key,
                    kb_config.embedding_model,
                    kb_config.embedding_base_url,
                ),
            )
            result: dict[str, object] = {
                "vector_kb_path": str(vector_store.path),
                "embedding_model": kb_config.embedding_model,
            }
            if args.rebuild_vector_kb:
                database_path = args.database or kb_config.database_path
                if not Path(database_path).expanduser().is_file():
                    raise ValueError(f"SQLite database does not exist: {database_path}")
                with SQLiteConnector(database_path) as connector:
                    database_tool = DatabaseTool(connector)
                    schemas = [
                        database_tool.describe_table(table)
                        for table in database_tool.list_tables()
                    ]
                history_store = SQLHistoryStore(kb_config.history_db_path)
                result["rebuild"] = KnowledgeBaseBuilder(vector_store).rebuild(
                    history_store=history_store,
                    schemas=schemas,
                    sources=args.kb_source,
                )
                result["sources"] = [str(Path(path).expanduser()) for path in args.kb_source]
            if args.kb_stats:
                result["stats"] = vector_store.stats()
        except Exception as exc:
            print(f"QueryForge failed: vector KB operation failed: {exc}", file=sys.stderr)
            return 1
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0

    history_action = any(
        (
            args.show_history,
            args.import_success_stories,
            args.import_reference_sql,
            args.clear_history,
        )
    )
    if history_action:
        if args.clear_history and not args.confirm_clear_history:
            print(
                "QueryForge failed: --clear-history requires "
                "--confirm-clear-history",
                file=sys.stderr,
            )
            return 2
        try:
            history_config = load_config(
                provider_override=args.model_provider,
                model_override=args.model,
            )
            store = SQLHistoryStore(history_config.history_db_path)
            result: dict[str, object] = {
                "history_db_path": str(store.database_path)
            }
            if args.clear_history:
                result["cleared"] = store.clear()
            if args.import_success_stories:
                result["success_stories"] = store.import_success_stories(
                    args.import_success_stories
                ).to_dict()
            if args.import_reference_sql:
                result["reference_sql"] = store.import_reference_sql(
                    args.import_reference_sql
                ).to_dict()
            if args.show_history:
                if args.history_limit < 1 or args.history_limit > 1000:
                    raise ValueError("--history-limit must be between 1 and 1000")
                result["entries"] = [
                    entry.to_dict()
                    for entry in store.list_entries(args.history_limit)
                ]
        except Exception as exc:
            print(f"QueryForge failed: history operation failed: {exc}", file=sys.stderr)
            return 1
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0

    if not args.question:
        print("QueryForge failed: --question is required", file=sys.stderr)
        return 2

    if args.auto_approve_plan and not args.plan_mode:
        print(
            "QueryForge failed: --auto-approve-plan requires --plan-mode",
            file=sys.stderr,
        )
        return 2
    if args.reset_session and not args.session_id:
        print(
            "QueryForge failed: --reset-session requires --session-id",
            file=sys.stderr,
        )
        return 2
    if args.new_session and args.reset_session:
        print(
            "QueryForge failed: --new-session and --reset-session cannot be combined",
            file=sys.stderr,
        )
        return 2
    if args.max_retries < 0 or args.max_retries > 10:
        print(
            "QueryForge failed: --max-retries must be between 0 and 10",
            file=sys.stderr,
        )
        return 2
    if args.history_top_k < 0 or args.history_top_k > 20:
        print(
            "QueryForge failed: --history-top-k must be between 0 and 20",
            file=sys.stderr,
        )
        return 2
    if args.vector_top_k < 0 or args.vector_top_k > 20:
        print(
            "QueryForge failed: --vector-top-k must be between 0 and 20",
            file=sys.stderr,
        )
        return 2

    try:
        selected_skills = _parse_skill_names(args.skills)
        options = AgentOptions(
                database=args.database,
                semantic_model_path=args.semantic_model,
                allow_schema_only=args.allow_schema_only,
                subject_tree_enabled=args.enable_subject_tree,
                subject_tree_path=args.subject_tree,
                subject=args.subject,
                default_subject=args.default_subject,
                sql_policy_path=args.sql_policy,
                model_provider=args.model_provider,
                model=args.model,
                skills=selected_skills,
                date_llm_fallback=args.date_llm_fallback,
                plan_mode=args.plan_mode,
                auto_approve_plan=args.auto_approve_plan,
                plan_approver=(
                    _interactive_plan_approver
                    if args.plan_mode and not args.auto_approve_plan
                    else None
                ),
                plan_presenter=_display_execution_plan if args.plan_mode else None,
                max_retries=args.max_retries,
                history_top_k=args.history_top_k,
                enable_vector_kb=args.enable_vector_kb,
                vector_top_k=args.vector_top_k,
                visualize=args.visualize,
                report=args.report,
                report_output_dir=args.report_output_dir,
                chart_output_dir=args.chart_output_dir,
                debug_prompts=args.debug_prompts,
                show_run_summary=args.show_run_summary,
                run_id=run_id,
                entrypoint="cli",
                session_id=args.session_id,
                new_session=args.new_session,
                reset_session=args.reset_session,
                tool_loop_enabled=args.tool_loop,
                tool_loop_max_rounds=args.tool_loop_max_rounds,
                tool_loop_timeout_seconds=args.tool_loop_timeout,
                tool_loop_preview_limit=args.tool_loop_preview_limit,
                parallel_candidates=args.parallel_candidates,
                parallel_max_preview=args.parallel_max_preview,
                parallel_preview_limit=args.parallel_preview_limit,
                parallel_preview_timeout_seconds=args.parallel_preview_timeout,
                complexity_mode=args.complexity_mode,
            )
        if args.stream:
            event_stream = service.stream(args.question, options)
            for event in event_stream:
                print(_format_stream_event(event), file=sys.stderr)
            if event_stream.error is not None:
                raise event_stream.error
            output = event_stream.result
            if output is None:
                raise RuntimeError("Streaming workflow ended without a final result")
        else:
            output = service.ask(args.question, options)
        if output.get("model_provider") or output.get("model"):
            print(
                f"Using model provider={output.get('model_provider')}, "
                f"model={output.get('model')}",
                file=sys.stderr,
            )
        vector_state = output.get("vector_kb", {})
        if args.enable_vector_kb and vector_state.get("status") == "degraded":
            print(
                f"Vector KB unavailable; continued with SQLite history retrieval: "
                f"{vector_state.get('error')}",
                file=sys.stderr,
            )
    except Exception as exc:  # CLI boundary: keep user-facing errors concise.
        print(f"QueryForge failed: {exc}", file=sys.stderr)
        return 1

    print(json.dumps(output, ensure_ascii=False, indent=2))
    return 0


def _parse_skill_names(value: str | None) -> list[str] | None:
    if value is None:
        return None
    mode = value.strip().lower()
    if mode == "auto":
        return None
    if mode in {"none", "off"}:
        return []
    names = list(dict.fromkeys(part.strip() for part in value.split(",") if part.strip()))
    if not names:
        raise ValueError("--skills must contain at least one skill name")
    return names


def _format_stream_event(event) -> str:
    """Render progress only; event payloads intentionally omit SQL and rows."""
    target = event.node_name or event.phase_name or event.artifact_type or "workflow"
    message = event.message or event.event_type
    return f"[{event.event_type}] {target}: {message}"


def _interactive_plan_approver(plan: ExecutionPlan) -> bool:
    print("Type yes to execute; no or Enter cancels [yes/no]:", file=sys.stderr)
    try:
        response = input()
    except EOFError:
        return False
    return response.strip().lower() == "yes"


def _display_execution_plan(plan: ExecutionPlan) -> None:
    print("QueryForge execution plan (SQL has not been executed):", file=sys.stderr)
    print(
        json.dumps(plan.model_dump(mode="json"), ensure_ascii=False, indent=2),
        file=sys.stderr,
    )


if __name__ == "__main__":
    raise SystemExit(main())
