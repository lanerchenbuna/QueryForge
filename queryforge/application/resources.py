"""Read-only application resources shared by REST, MCP, CLI, and Gateway."""

from __future__ import annotations

import importlib.util
from contextlib import contextmanager
from pathlib import Path
from typing import Callable, Iterator

from queryforge.core.config import Config, list_model_definitions
from queryforge.domain.security import load_sql_policy
from queryforge.domain.semantic import SemanticModelContext, SemanticModelLoader, SubjectTreeLoader
from queryforge.domain.skills import SkillRegistry
from queryforge.infrastructure.db.sqlite_connector import SQLiteConnector
from queryforge.infrastructure.storage import SQLHistoryStore
from queryforge.infrastructure.tools.database_tool import DatabaseTool
from queryforge.interfaces.transport_security import (
    validate_database_path,
    validate_report_root,
)
from queryforge.orchestration.orchestrator.pipeline_registry import describe_pipelines
from queryforge.workflow.workflow_runner import WorkflowRunner


class ResourceService:
    """Expose governed metadata and preview operations without workflow execution."""

    def __init__(
        self,
        config_loader: Callable[..., Config],
        skill_registry: SkillRegistry,
    ) -> None:
        self.config_loader = config_loader
        self.skill_registry = skill_registry

    def list_models(
        self,
        model_provider: str | None = None,
        model: str | None = None,
    ) -> list[dict]:
        active = self.config_loader(
            provider_override=model_provider,
            model_override=model,
        )
        return [
            {
                "name": definition.name,
                "type": definition.type,
                "model": (
                    active.llm_model
                    if definition.name == active.llm_provider
                    else definition.model
                ),
                "base_url": definition.base_url,
                "active": definition.name == active.llm_provider,
            }
            for definition in list_model_definitions()
        ]

    def list_subjects(self, subject_tree_path: str | None = None) -> list[dict]:
        config = self.config_loader()
        path = subject_tree_path or config.subject_tree_path
        if not path:
            raise ValueError("subject_tree_path is required to list subjects")
        tree = SubjectTreeLoader.load(path)
        return [
            {
                "id": subject.id,
                "name": subject.name,
                "description": subject.description,
                "tables": subject.tables,
                "metrics": subject.metrics,
                "priority": subject.priority,
                "default": subject.id == tree.default_subject,
            }
            for subject in tree.subjects
        ]

    def report_path(
        self, run_id: str, report_output_dir: str | None = None
    ) -> Path:
        if not run_id or any(
            char
            not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-"
            for char in run_id
        ):
            raise ValueError("run_id contains unsafe path characters")
        config = self.config_loader()
        root = Path(
            report_output_dir or config.report_output_dir
        ).expanduser().resolve()
        validate_report_root(config, report_output_dir)
        path = root / f"{run_id}.html"
        if not path.is_file():
            raise ValueError(f"Report does not exist for run_id {run_id!r}")
        return path

    def list_skills(self) -> list[dict]:
        return [
            {
                "name": skill.name,
                "description": skill.metadata.description,
                "allowed_nodes": list(skill.metadata.allowed_nodes),
                "enabled": skill.metadata.enabled,
                "priority": skill.metadata.priority,
            }
            for skill in self.skill_registry.list_skills()
        ]

    def get_history(self, limit: int = 20) -> dict:
        if limit < 1 or limit > 1000:
            raise ValueError("History limit must be between 1 and 1000")
        store = SQLHistoryStore(self.config_loader().history_db_path)
        return {
            "history_db_path": str(store.database_path),
            "entries": [entry.to_dict() for entry in store.list_entries(limit)],
        }

    def list_tables(
        self,
        database: str | None = None,
        sql_policy_path: str | None = None,
    ) -> list[dict]:
        with self.database_tool(database, sql_policy_path) as tool:
            return [
                {
                    "name": schema.table_name,
                    "column_count": len(schema.columns),
                    "foreign_key_count": len(schema.foreign_keys),
                }
                for schema in (tool.describe_table(name) for name in tool.list_tables())
            ]

    def describe_table(
        self,
        table_name: str,
        database: str | None = None,
        sql_policy_path: str | None = None,
        sample_limit: int = 5,
    ) -> dict:
        if sample_limit < 1 or sample_limit > 100:
            raise ValueError("sample_limit must be between 1 and 100")
        with self.database_tool(database, sql_policy_path) as tool:
            schema = tool.describe_table(table_name)
            preview = tool.execute_sql_preview(
                f'SELECT * FROM "{schema.table_name}"',
                sample_limit,
            )
            return {
                "table": schema.model_dump(mode="json"),
                "sample_columns": preview.columns,
                "sample_rows": preview.rows,
                "sample_row_count": preview.row_count,
            }

    def list_metrics(
        self,
        database: str | None = None,
        semantic_model_path: str | None = None,
    ) -> list[dict]:
        return [
            metric.model_dump(mode="json")
            for metric in self.semantic_model_context(
                database, semantic_model_path
            ).model.metrics
        ]

    def get_metric(
        self,
        metric_name: str,
        database: str | None = None,
        semantic_model_path: str | None = None,
    ) -> dict:
        metric = next(
            (
                item
                for item in self.list_metrics(database, semantic_model_path)
                if item["name"] == metric_name
            ),
            None,
        )
        if metric is None:
            raise ValueError(f"Unknown metric {metric_name!r}")
        return metric

    def preview_sql(
        self,
        sql: str,
        database: str | None = None,
        sql_policy_path: str | None = None,
        limit: int = 20,
    ) -> dict:
        with self.database_tool(database, sql_policy_path) as tool:
            result = tool.execute_sql_preview(sql, limit)
            decision = tool.last_policy_decision
            return {
                "columns": result.columns,
                "rows": result.rows,
                "row_count": result.row_count,
                "policy_decision": (
                    decision.model_dump(mode="json") if decision else None
                ),
            }

    def health(self) -> dict:
        return {
            "status": "ok",
            "service": "QueryForge",
            "workflow": WorkflowRunner.NODE_NAMES,
            "orchestration": {
                "mode": "agent_team",
                "pipelines": describe_pipelines(),
            },
            "optional_dependencies": {
                "fastapi": importlib.util.find_spec("fastapi") is not None,
                "mcp": importlib.util.find_spec("mcp") is not None,
                "lancedb": importlib.util.find_spec("lancedb") is not None,
            },
        }

    @contextmanager
    def database_tool(
        self,
        database: str | None,
        sql_policy_path: str | None,
    ) -> Iterator[DatabaseTool]:
        config = self.config_loader()
        validate_database_path(config, database)
        path = Path(database or config.database_path).expanduser()
        if not path.is_file():
            raise ValueError(f"SQLite database does not exist: {path}")
        with SQLiteConnector(str(path)) as connector:
            policy, source = load_sql_policy(
                sql_policy_path or config.sql_policy_path
            )
            yield DatabaseTool(connector, policy, policy_source_path=source)

    def semantic_model_context(
        self,
        database: str | None,
        semantic_model_path: str | None,
    ) -> SemanticModelContext:
        config = self.config_loader()
        model_path = semantic_model_path or config.semantic_model_path
        if not model_path:
            raise ValueError("semantic_model_path is required for metric resources")
        with self.database_tool(database, None) as tool:
            schemas = [
                tool.describe_table_for_validation(name) for name in tool.list_tables()
            ]
        return SemanticModelLoader.load_and_validate(model_path, schemas, "")
