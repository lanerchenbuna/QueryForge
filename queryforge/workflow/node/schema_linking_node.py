"""Load the policy-filtered SQLite schema that the current question needs."""

import inspect
import logging
import re
from functools import lru_cache
from typing import Any, Iterable

from queryforge.workflow.node.base import Node
from queryforge.core.schemas.models import ColumnValueHint, Context, NodeResult, VectorMatch
from queryforge.domain.knowledge import VerificationLevel, verification_level_of
from queryforge.domain.semantic import (
    SchemaRetrievalResult,
    SchemaRetriever,
    SemanticModelContext,
    SemanticModelLoader,
)
from queryforge.infrastructure.storage import KnowledgeBaseBuilder, VectorStore
from queryforge.infrastructure.storage.vector_store import document_matches_filters
from queryforge.infrastructure.tools.database_tool import DatabaseTool


LOGGER = logging.getLogger("queryforge.schema")


SQL_EXAMPLE_SOURCE_TYPES = (
    "sql_history",
    "reference_sql",
    "reference_template",
    "success_story",
)


@lru_cache(maxsize=None)
def _accepts_filters(store_type: type) -> bool:
    """Whether a vector store implements the step-13 ``filters`` keyword.

    Stores written against the pre-step-13 interface keep working: when they do
    not accept ``filters``, retrieval still runs and the evidence records that the
    governance filter was not pushable instead of failing the node.
    """
    method = getattr(store_type, "search", None)
    if method is None:
        return False
    try:
        parameters = inspect.signature(method).parameters.values()
    except (TypeError, ValueError):  # pragma: no cover - builtins
        return False
    return any(
        parameter.name == "filters"
        or parameter.kind is inspect.Parameter.VAR_KEYWORD
        for parameter in parameters
    )


class SchemaLinkingNode(Node):
    name = "schema_linking"
    description = "Read table metadata relevant to the question"
    MAX_VALUE_HINTS = 20
    VALUES_PER_COLUMN = 3
    CANDIDATE_MULTIPLIER = 3
    DEFAULT_MAX_CONTEXT_CHARS = 4000
    #: Lower number wins a tie on similarity: reviewed metric knowledge first,
    #: then glossary, schema docs, business documents, history, unrecognized.
    SOURCE_PRIORITY = {
        "metric_knowledge": 0,
        "glossary": 1,
        "schema_doc": 2,
        "knowledge_document": 3,
        "sql_history": 4,
        "reference_sql": 4,
        "reference_template": 4,
        "success_story": 4,
    }
    #: Documents that try to talk to the model are still *data*: they are flagged
    #: and reported, never executed as policy or tool instructions.
    INSTRUCTION_PATTERNS = (
        re.compile(r"ignore\s+(?:all\s+)?(?:the\s+)?(?:previous|prior|above|system)", re.I),
        re.compile(r"忽略(?:之前|上述|以上|系统)", re.I),
        re.compile(r"(?:new|updated)\s+system\s+prompt", re.I),
        re.compile(r"you\s+are\s+now\s+(?:a|an|the)\b", re.I),
        re.compile(r"\b(?:grant|elevate|escalate)\s+(?:permissions?|access|privileges)", re.I),
        re.compile(r"\bbypass\s+(?:the\s+)?(?:policy|policies|guard|security|rules?)", re.I),
        re.compile(r"\b(?:drop|truncate|delete)\s+table\b", re.I),
        re.compile(r"\b(?:leak|exfiltrate|reveal|dump)\b[^.\n]{0,40}\b(?:history|secrets?|tokens?|passwords?)", re.I),
    )
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
        schema_retriever: SchemaRetriever | None = None,
        *,
        domain_id: str | None = None,
        data_version: str | None = None,
        version: str | None = None,
        permissions: Iterable[str] = (),
        max_context_documents: int | None = None,
        max_context_chars: int = DEFAULT_MAX_CONTEXT_CHARS,
    ) -> None:
        self.database_tool = database_tool
        self.vector_store = vector_store
        self.vector_top_k = vector_top_k
        self.semantic_model_path = semantic_model_path
        self.schema_retriever = schema_retriever or SchemaRetriever()
        # Governance scope for retrieval. A caller may also publish it through
        # ``context.task_context["retrieval_scope"]``, which takes precedence.
        self.domain_id = domain_id
        self.data_version = data_version
        self.version = version
        self.permissions = tuple(str(item) for item in permissions if str(item).strip())
        self.max_context_documents = max_context_documents
        self.max_context_chars = int(max_context_chars)

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
            scoped_schemas = [
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
                    scoped_schemas,
                )
            # Step 05: subject scoping happens first, retrieval second.
            retrieval = self._retrieve(context, scoped_schemas)
            context.relevant_tables = self._context_schemas(
                context, retrieval, scoped_schemas
            )
            self._record_retrieval(context, retrieval)
            # History/reference scoping keeps its existing subject-scope semantics
            # (retrieval narrows the generation context, not the audit trail).
            self._scope_retrieval(context, set(tables))
            keywords = self._question_keywords(context.task.question)
            context.value_hints = self._collect_value_hints(
                context, context.relevant_tables, keywords
            )
            if self.vector_store is not None and self.vector_top_k > 0:
                self._retrieve_vector_context(context)
            elif self.vector_store is None:
                self._record_lexical_fallback(
                    context, reason="vector_store_not_configured", status="disabled"
                )
            else:
                # An explicit vector_top_k <= 0 keeps its pre-step-13 meaning:
                # retrieval is off, so no vector candidates are fabricated.
                self._record_lexical_fallback(
                    context, reason="vector_top_k_disabled", status="disabled"
                )
            self._record_vector_status(context)
        except Exception as exc:
            return self.failure(f"Could not inspect database schema: {exc}")
        return self.success(
            f"Loaded schemas for {len(context.relevant_tables)} table(s) and "
            f"{len(context.value_hints)} value hint(s); "
            f"{len(context.vector_schema_matches)} vector schema match(es); "
            f"schema retrieval "
            f"{context.task_context.get('schema_retrieval', {}).get('mode', 'unknown')}; "
            f"semantic model {'active' if context.semantic_model else 'disabled'}"
        )

    def _retrieve(
        self, context: Context, scoped_schemas: list
    ) -> SchemaRetrievalResult:
        """Run deterministic schema retrieval, never failing the node outright."""
        subject = (
            context.subject_selection.subject
            if context.subject_selection is not None
            and context.subject_selection.status == "selected"
            else None
        )
        try:
            return self.schema_retriever.retrieve(
                scoped_schemas,
                context.task.question,
                semantic_model=context.semantic_model,
                metric_matches=context.metric_matches or None,
                metric_join_paths=context.metric_join_paths or None,
                requested_dimensions=context.metric_requested_dimensions or None,
                subject_tables=subject.tables if subject is not None else None,
            )
        except Exception as exc:  # pragma: no cover - defensive degradation
            LOGGER.warning("schema_retrieval_failed error=%s", exc)
            return SchemaRetrievalResult(
                mode="passthrough",
                selected_tables=list(scoped_schemas),
                required_tables=[schema.table_name for schema in scoped_schemas],
                evidence={
                    "mode": "passthrough",
                    "reason": "schema_retrieval_failed",
                    "error": str(exc),
                    "selected_table_names": [
                        schema.table_name for schema in scoped_schemas
                    ],
                    "degradation": ["schema_retrieval_failed"],
                },
            )

    @staticmethod
    def _context_schemas(
        context: Context,
        retrieval: SchemaRetrievalResult,
        scoped_schemas: list,
    ) -> list:
        """Project the retrieval selection onto the policy-filtered schema.

        Semantic-model ``hidden_columns`` is a prompt-hiding directive (enforced
        by ``GenSqlNode._build_prompt``), not a removal from the physical schema,
        so hidden columns stay part of ``context.relevant_tables`` exactly as
        before step 05 while remaining excluded from the prompt.
        """
        if retrieval.mode == "passthrough":
            return list(retrieval.selected_tables)
        physical = {schema.table_name: schema for schema in scoped_schemas}
        hidden = (
            context.semantic_model.hidden_column_refs()
            if context.semantic_model
            else set()
        )
        projected = []
        for selected in retrieval.selected_tables:
            schema = physical.get(selected.table_name)
            if schema is None:
                continue
            keep = {column.name for column in selected.columns}
            keep.update(
                column for table, column in hidden if table == schema.table_name
            )
            if len(keep) == len(schema.columns):
                projected.append(schema)
                continue
            projected.append(
                schema.model_copy(
                    update={
                        "columns": [
                            column
                            for column in schema.columns
                            if column.name in keep
                        ]
                    }
                )
            )
        return projected

    @staticmethod
    def _record_retrieval(
        context: Context, retrieval: SchemaRetrievalResult
    ) -> None:
        evidence = dict(retrieval.evidence)
        evidence.setdefault("selected_table_names", retrieval.selected_table_names)
        context.task_context["schema_retrieval"] = evidence

    @staticmethod
    def _record_vector_status(context: Context) -> None:
        evidence = context.task_context.get("schema_retrieval")
        if isinstance(evidence, dict):
            evidence["vector_kb_status"] = context.vector_kb_status

    # ---------------------------------------------------- governed retrieval
    def _retrieval_scope(self, context: Context) -> dict[str, Any]:
        """Resolve the domain/version/permission scope used for filtering."""
        scope: dict[str, Any] = {
            "domain_id": self.domain_id,
            "data_version": self.data_version,
            "version": self.version,
            "permissions": list(self.permissions),
        }
        published = context.task_context.get("retrieval_scope")
        if isinstance(published, dict):
            for key in ("domain_id", "data_version", "version"):
                value = published.get(key)
                if isinstance(value, str) and value.strip():
                    scope[key] = value.strip()
            if isinstance(published.get("permissions"), (list, tuple)):
                scope["permissions"] = [
                    str(item) for item in published["permissions"] if str(item).strip()
                ]
        return scope

    @classmethod
    def _vector_filters(cls, scope: dict[str, Any]) -> dict[str, Any]:
        """Translate a retrieval scope into vector-store governance filters.

        ``data_version`` is deliberately *not* part of this shared filter: it scopes
        executed SQL examples (a data snapshot), not governed definitions, so
        applying it here would exclude every metric and glossary document. It is
        applied to the example channel only (see ``_example_filters``).
        """
        filters: dict[str, Any] = {}
        if scope.get("domain_id"):
            filters["domain_id"] = scope["domain_id"]
        if scope.get("version"):
            filters["version"] = scope["version"]
        if scope.get("permissions"):
            filters["permissions"] = list(scope["permissions"])
        return filters

    @classmethod
    def _example_filters(
        cls, scope: dict[str, Any], filters: dict[str, Any]
    ) -> dict[str, Any]:
        """Example-channel filters: a data version binds executed SQL examples."""
        example_filters = dict(filters)
        data_version = scope.get("data_version")
        if data_version:
            example_filters["data_version"] = data_version
        return example_filters

    def _budget_documents(self) -> int:
        if self.max_context_documents is not None:
            return max(int(self.max_context_documents), 1)
        return max(self.vector_top_k * 2, 4)

    def _retrieve_vector_context(self, context: Context) -> None:
        """Filter → recall → dedupe/rerank → budget, degrading to lexical only.

        Order of operations mirrors the store contract: governance filters are
        applied to candidates *before* ranking and the top-k cut, so a document
        from another domain/user can never displace a legal candidate. A store
        that cannot push the filters down still gets the local check (never a
        fail-open skip), but the scope then cannot be reported as enforced — the
        run is marked ``degraded`` with ``filter_pushdown_unsupported``. Failures
        never fail the node: the vector channel degrades and the lexical history
        path keeps providing evidence, with the reason recorded.
        """
        scope = self._retrieval_scope(context)
        filters = self._vector_filters(scope)
        supports_filters = _accepts_filters(type(self.vector_store))
        # A store that cannot push the scope down leaves only the local check,
        # which runs *after* the store's own candidate selection and top-k cut:
        # out-of-scope documents can still displace legal candidates, so the
        # scope is not fully enforced and the run must report that instead of an
        # active control (step 13, 13-I1).
        unenforced = (
            "filter_pushdown_unsupported" if filters and not supports_filters else None
        )
        evidence = self._vector_evidence(context, scope, filters, supports_filters)
        try:
            self._write_schema_documents(context, scope)
            candidate_k = max(self.vector_top_k, 1) * self.CANDIDATE_MULTIPLIER
            search_kwargs: dict[str, Any] = {
                "top_k": candidate_k,
                "source_types": (
                    "schema_doc",
                    "metric_knowledge",
                    "glossary",
                    "knowledge_document",
                ),
            }
            if supports_filters and filters:
                search_kwargs["filters"] = filters
            doc_matches = [
                VectorMatch.model_validate(match.to_dict())
                for match in self.vector_store.search(
                    context.task.question, **search_kwargs
                )
            ]
            examples = list(context.vector_sql_matches)
            evidence["candidates"]["documents"] = len(doc_matches)
            evidence["candidates"]["examples"] = len(examples)
            context.vector_schema_matches = self._governed_selection(
                doc_matches, evidence, channel="documents"
            )
            context.vector_sql_matches = self._governed_selection(
                examples, evidence, channel="examples"
            )
            context.vector_kb_status = "degraded" if unenforced else "active"
            if unenforced is not None:
                evidence["status"] = "degraded"
                evidence["reason"] = unenforced
        except Exception as exc:
            context.vector_kb_status = "degraded"
            context.vector_kb_error = str(exc)
            evidence["status"] = "degraded"
            evidence["reason"] = "vector_retrieval_failed"
            evidence["error"] = str(exc)
            self._record_lexical_fallback(
                context, reason="vector_retrieval_failed", status="degraded"
            )
            LOGGER.warning("schema_vector_search_failed error=%s", exc)

    def _write_schema_documents(
        self, context: Context, scope: dict[str, Any] | None = None
    ) -> None:
        """Publish schema docs idempotently (content hashes avoid re-embedding).

        Documents are stamped with the *resolved* retrieval scope — the same
        scope the read path filters on — not with the constructor kwargs alone.
        The production runner never passes the scope kwargs: it publishes
        ``context.task_context["retrieval_scope"]``, so writing with the
        constructor-only scope stamped ``domain_id=None`` on every document and
        the identical filter then dropped all of them from the very run that
        wrote them (a domain-bound run retrieved nothing while reporting an
        active, governed control). Constructor kwargs remain the fallback that
        ``_retrieval_scope`` resolves when nothing is published.
        """
        resolved = self._retrieval_scope(context) if scope is None else scope
        documents = KnowledgeBaseBuilder.schema_documents(
            context.relevant_tables,
            domain_id=resolved.get("domain_id"),
            data_version=resolved.get("data_version"),
            version=resolved.get("version"),
        )
        upsert = getattr(self.vector_store, "upsert_documents", None)
        if callable(upsert):
            upsert(documents)
            return
        self.vector_store.add_documents(documents)  # pragma: no cover - legacy store

    def _governed_selection(
        self,
        matches: list[VectorMatch],
        evidence: dict[str, Any],
        *,
        channel: str,
    ) -> list[VectorMatch]:
        """Dedupe, rerank, and budget one retrieval channel."""
        filters = (
            evidence.get("example_filters") or {}
            if channel == "examples"
            else evidence.get("filters") or {}
        )
        budget_documents = self._budget_documents()
        kept: list[VectorMatch] = []
        seen_ids: set[str] = set()
        seen_text: set[str] = set()
        for match in matches:
            # The local check always runs: it is the *only* governance filter
            # left when the store cannot push ``filters`` down, and skipping it
            # exactly then would fail open for every candidate the ungoverned
            # store chose to return. ``filter_support`` stays diagnostic.
            if filters and not document_matches_filters(match, filters):
                evidence["dropped"]["filters"] += 1
                continue
            if match.id in seen_ids:
                evidence["dropped"]["duplicates"] += 1
                continue
            digest = re.sub(r"\s+", " ", match.text.strip().lower())
            if digest and digest in seen_text:
                evidence["dropped"]["duplicates"] += 1
                continue
            if self._is_dropped_example(match):
                # Kept out of the few-shot context; still available for diagnostics.
                evidence["dropped"]["unverified_examples"] += 1
                evidence["unverified_diagnostics"].append(match.id)
                continue
            seen_ids.add(match.id)
            seen_text.add(digest)
            match = self._flag_instruction_like(match, evidence)
            kept.append(match)
        ranked = sorted(kept, key=self._rank_key)
        selected: list[VectorMatch] = []
        used_chars = 0
        for match in ranked:
            if len(selected) >= budget_documents:
                evidence["dropped"]["budget"] += 1
                continue
            if selected and used_chars + len(match.text) > self.max_context_chars:
                evidence["dropped"]["budget"] += 1
                continue
            used_chars += len(match.text)
            selected.append(match)
        evidence["returned"][channel] = len(selected)
        evidence["budget"]["used_documents"] += len(selected)
        evidence["budget"]["used_chars"] += used_chars
        return selected

    def _is_dropped_example(self, match: VectorMatch) -> bool:
        """Drop explicitly ``unverified`` SQL examples from few-shot context.

        Only an explicit ``unverified`` label is dropped: an unlabelled legacy
        document keeps its pre-step-13 behaviour (it is simply never treated as a
        trusted example by ``is_trusted_for_examples``). Successful execution is
        not a drop reason — that would discard useful material — but it is also
        never promoted to trust.
        """
        metadata = match.metadata or {}
        if "verification_level" not in metadata:
            return False
        if match.source_type not in SQL_EXAMPLE_SOURCE_TYPES:
            return False
        return (
            verification_level_of(metadata.get("verification_level"))
            is VerificationLevel.unverified
        )

    def _flag_instruction_like(
        self, match: VectorMatch, evidence: dict[str, Any]
    ) -> VectorMatch:
        """Mark document text that tries to instruct the model; content stays data."""
        if not any(pattern.search(match.text) for pattern in self.INSTRUCTION_PATTERNS):
            return match
        evidence["instruction_like_documents"].append(match.id)
        metadata = {**(match.metadata or {})}
        metadata["content_role"] = "instruction_like_data"
        # No policy or tool permission is derived from document text; the record
        # exists so a reviewer can see the injection attempt.
        return match.model_copy(update={"metadata": metadata})

    @classmethod
    def _rank_key(cls, match: VectorMatch) -> tuple:
        """Rerank order: similarity, then source priority, then review state, then id."""
        score = match.score if match.score is not None else 0.0
        priority = cls.SOURCE_PRIORITY.get(match.source_type, 9)
        metadata = match.metadata or {}
        review_rank = 0 if str(metadata.get("review_status")) == "reviewed" else 1
        return (-float(score), priority, review_rank, match.id)

    def _vector_evidence(
        self,
        context: Context,
        scope: dict[str, Any],
        filters: dict[str, Any],
        supports_filters: bool,
    ) -> dict[str, Any]:
        evidence = {
            "status": "active",
            "scope": scope,
            "filters": filters,
            "example_filters": self._example_filters(scope, filters),
            "filter_support": "native" if supports_filters else "unsupported_store",
            # ``scope_enforced`` is False only when a scope was requested and the
            # store could not apply it before its own selection: the local check
            # still ran, but the store's top-k window was already chosen without
            # the scope, so the control is reported as unenforced (see the
            # ``degraded`` status the caller sets in that case).
            "enforcement": {
                "filters_requested": bool(filters),
                "pushed_down": bool(filters) and supports_filters,
                "local_filter_applied": bool(filters),
                "scope_enforced": supports_filters or not filters,
                "reason": (
                    None
                    if supports_filters or not filters
                    else "filter_pushdown_unsupported"
                ),
            },
            "candidates": {"documents": 0, "examples": 0},
            "returned": {"documents": 0, "examples": 0},
            "dropped": {
                "filters": 0,
                "duplicates": 0,
                "unverified_examples": 0,
                "budget": 0,
            },
            "budget": {
                "max_documents": self._budget_documents(),
                "max_chars": self.max_context_chars,
                "used_documents": 0,
                "used_chars": 0,
            },
            "rerank": {
                "order": ["score", "source_priority", "review_status", "id"],
                "source_priority": dict(self.SOURCE_PRIORITY),
                "applied": True,
            },
            "unverified_diagnostics": [],
            "instruction_like_documents": [],
            "policy_effect": "none",
            "lexical_fallback": {"used": False, "reason": None, "count": 0, "matches": []},
        }
        retrieval = context.task_context.get("schema_retrieval")
        if isinstance(retrieval, dict):
            retrieval["vector_retrieval"] = evidence
        return evidence

    def _record_lexical_fallback(
        self,
        context: Context,
        *,
        reason: str,
        status: str,
    ) -> None:
        """Record bounded lexical retrieval used when the vector channel is gone."""
        evidence = context.task_context.get("schema_retrieval")
        if not isinstance(evidence, dict):
            return
        retrieval = evidence.get("vector_retrieval")
        if not isinstance(retrieval, dict):
            retrieval = {"status": status, "reason": reason}
            evidence["vector_retrieval"] = retrieval
        budget_documents = self._budget_documents()
        matches = [
            {
                "id": f"lexical:{match.id}",
                "question": match.question,
                "similarity": match.similarity,
                "source": match.source,
                "verification_level": VerificationLevel.execution_success.value,
            }
            for match in (context.history_matches or [])[:budget_documents]
        ]
        retrieval["status"] = status
        retrieval["reason"] = retrieval.get("reason") or reason
        retrieval["lexical_fallback"] = {
            "used": bool(matches),
            "reason": reason,
            "count": len(matches),
            "bounded_by": budget_documents,
            "matches": matches,
        }
        if not isinstance(retrieval.get("budget"), dict):
            retrieval["budget"] = {
                "max_documents": budget_documents,
                "max_chars": self.max_context_chars,
                "used_documents": 0,
                "used_chars": 0,
            }

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
        self, context: Context, schemas: list, keywords: list[str]
    ) -> list[ColumnValueHint]:
        hidden_columns = (
            context.semantic_model.hidden_column_refs()
            if context.semantic_model
            else set()
        )
        hints: list[ColumnValueHint] = []
        for schema in schemas:
            for column in schema.columns:
                if len(hints) >= self.MAX_VALUE_HINTS:
                    return hints
                if (schema.table_name, column.name) in hidden_columns:
                    # Step 05: never sample values from governance-hidden columns.
                    continue
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
