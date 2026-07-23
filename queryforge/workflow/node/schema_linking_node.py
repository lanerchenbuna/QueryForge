"""Load all SQLite table schemas into the shared context."""

import logging
import re

from queryforge.workflow.node.base import Node
from queryforge.core.schemas.models import ColumnValueHint, Context, NodeResult, VectorMatch
from queryforge.domain.semantic import SemanticModelContext, SemanticModelLoader
from queryforge.infrastructure.storage import KnowledgeBaseBuilder, VectorStore
from queryforge.infrastructure.tools.database_tool import DatabaseTool


LOGGER = logging.getLogger("queryforge.schema")


class SchemaLinkingNode(Node):
    name = "schema_linking"
    description = "Read table metadata relevant to the question"
    MAX_VALUE_HINTS = 20
    VALUES_PER_COLUMN = 3
    _QUESTION_STOPWORDS = {
        "about",
        "after",
        "among",
        "and",
        "are",
        "before",
        "does",
        "for",
        "free",
        "from",
        "give",
        "have",
        "highest",
        "how",
        "into",
        "list",
        "lowest",
        "more",
        "please",
        "rate",
        "rates",
        "school",
        "schools",
        "show",
        "student",
        "students",
        "that",
        "the",
        "their",
        "three",
        "than",
        "this",
        "what",
        "where",
        "which",
        "with",
    }

    def __init__(
        self,
        database_tool: DatabaseTool,
        vector_store: VectorStore | None = None,
        vector_top_k: int = 3,
        semantic_model_path: str | None = None,
    ) -> None:
        self.database_tool = database_tool
        self.vector_store = vector_store
        self.vector_top_k = vector_top_k
        self.semantic_model_path = semantic_model_path

    def execute(self, context: Context) -> NodeResult:
        try:
            all_tables = self.database_tool.list_tables()
            if not all_tables:
                return self.failure("The SQLite database contains no user tables")
            all_schemas = [
                self.database_tool.describe_table(table) for table in all_tables
            ]
            if self.semantic_model_path:
                context.semantic_model = SemanticModelLoader.load_and_validate(
                    self.semantic_model_path,
                    [
                        self.database_tool.describe_table_for_validation(table)
                        for table in all_tables
                    ],
                    context.task.question,
                )
            tables = self._scoped_table_names(context, all_tables)
            context.relevant_tables = [
                schema for schema in all_schemas if schema.table_name in tables
            ]
            if context.semantic_model:
                context.semantic_model = self._scope_semantic_model(
                    context.semantic_model,
                    context,
                    tables,
                )
                SemanticModelLoader.validate_policy_visibility(
                    context.semantic_model.model,
                    context.relevant_tables,
                )
            self._scope_retrieval(context, set(tables))
            keywords = self._question_keywords(context.task.question)
            context.value_hints = self._collect_value_hints(
                context.relevant_tables, keywords
            )
            if self.vector_store is not None:
                try:
                    self.vector_store.add_documents(
                        KnowledgeBaseBuilder.schema_documents(context.relevant_tables)
                    )
                    context.vector_schema_matches = [
                        VectorMatch.model_validate(match.to_dict())
                        for match in self.vector_store.search(
                            context.task.question,
                            top_k=self.vector_top_k,
                            source_types=("schema_doc",),
                        )
                    ]
                except Exception as exc:
                    context.vector_kb_status = "degraded"
                    context.vector_kb_error = str(exc)
                    LOGGER.warning("schema_vector_search_failed error=%s", exc)
        except Exception as exc:
            return self.failure(f"Could not inspect database schema: {exc}")
        return self.success(
            f"Loaded schemas for {len(tables)} table(s) and "
            f"{len(context.value_hints)} value hint(s); "
            f"{len(context.vector_schema_matches)} vector schema match(es); "
            f"semantic model {'active' if context.semantic_model else 'disabled'}"
        )

    @staticmethod
    def _scoped_table_names(context: Context, all_tables: list[str]) -> list[str]:
        selection = context.subject_selection
        if (
            selection is None
            or selection.status != "selected"
            or selection.subject is None
        ):
            return all_tables
        requested = set(selection.subject.tables)
        if context.semantic_model:
            entity_tables = {
                entity.table
                for entity in context.semantic_model.model.entities
                if entity.name in selection.subject.entities
            }
            requested.update(entity_tables)
        scoped = [table for table in all_tables if table in requested]
        if scoped:
            return scoped
        selection.status = "fallback_all"
        selection.fallback_reason = "subject_has_no_tables_in_database"
        selection.reason = (
            "Selected subject has no available tables in this database; "
            "expanded to the full schema."
        )
        return all_tables

    @staticmethod
    def _scope_semantic_model(
        semantic_context: SemanticModelContext,
        context: Context,
        scoped_tables: list[str],
    ) -> SemanticModelContext:
        selection = context.subject_selection
        if (
            selection is None
            or selection.status != "selected"
            or selection.subject is None
        ):
            return semantic_context
        subject = selection.subject
        table_set = set(scoped_tables)
        entity_names = set(subject.entities)
        entities = [
            entity
            for entity in semantic_context.model.entities
            if entity.table in table_set or entity.name in entity_names
        ]
        entity_names = {entity.name for entity in entities}
        metrics = [
            metric
            for metric in semantic_context.model.metrics
            if (
                metric.name in subject.metrics
                if subject.metrics
                else metric.entity in entity_names
            )
        ]
        relationships = [
            relationship
            for relationship in semantic_context.model.relationships
            if all(
                reference.split(".", 1)[0] in table_set
                for reference in (relationship.from_ref, relationship.to_ref)
            )
        ]
        join_paths = [
            path
            for path in semantic_context.model.join_paths
            if path.from_entity in entity_names and path.to_entity in entity_names
        ]
        model = semantic_context.model.model_copy(
            update={
                "entities": entities,
                "metrics": metrics,
                "relationships": relationships,
                "join_paths": join_paths,
            }
        )
        return semantic_context.model_copy(
            update={
                "model": model,
                "matches": [
                    match
                    for match in semantic_context.matches
                    if match.table in table_set
                ],
            }
        )

    @staticmethod
    def _scope_retrieval(context: Context, scoped_tables: set[str]) -> None:
        """Keep only prior examples whose declared tables are usable in this scope."""
        selection = context.subject_selection
        if selection is None or selection.status != "selected":
            return
        normalized_tables = {table.lower() for table in scoped_tables}
        context.history_matches = [
            match
            for match in context.history_matches
            if {table.lower() for table in match.tables_used}.issubset(
                normalized_tables
            )
        ]
        context.reference_examples = [
            example
            for example in context.reference_examples
            if SchemaLinkingNode._sql_tables(example.sql).issubset(normalized_tables)
        ]
        knowledge_sources = {
            source.lower()
            for source in selection.subject.knowledge_sources
        } if selection.subject else set()
        if knowledge_sources:
            context.vector_sql_matches = [
                match
                for match in context.vector_sql_matches
                if any(
                    source in f"{match.id}\n{match.text}".lower()
                    for source in knowledge_sources
                )
            ]

    @staticmethod
    def _sql_tables(sql: str) -> set[str]:
        return {
            match.lower()
            for match in re.findall(
                r'\b(?:from|join)\s+[`"\[]?([A-Za-z_][A-Za-z0-9_]*)',
                sql,
                flags=re.IGNORECASE,
            )
        }

    def _collect_value_hints(
        self, schemas: list, keywords: list[str]
    ) -> list[ColumnValueHint]:
        hints: list[ColumnValueHint] = []
        for schema in schemas:
            for column in schema.columns:
                if len(hints) >= self.MAX_VALUE_HINTS:
                    return hints
                column_type = column.data_type.upper()
                if column_type and not any(
                    text_type in column_type
                    for text_type in ("CHAR", "CLOB", "TEXT", "VARCHAR")
                ):
                    continue
                values = self.database_tool.find_matching_values(
                    schema.table_name,
                    column.name,
                    keywords,
                    self.VALUES_PER_COLUMN,
                )
                if values:
                    hints.append(
                        ColumnValueHint(
                            table_name=schema.table_name,
                            column_name=column.name,
                            values=values,
                        )
                    )
        return hints

    @classmethod
    def _question_keywords(cls, question: str) -> list[str]:
        tokens = {
            token.lower()
            for token in re.findall(r"[A-Za-z][A-Za-z0-9_-]+", question)
            if len(token) >= 4
        }
        useful = tokens - cls._QUESTION_STOPWORDS
        return sorted(useful, key=lambda token: (-len(token), token))[:12]
