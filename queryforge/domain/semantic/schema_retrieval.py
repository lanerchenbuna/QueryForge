"""Deterministic schema retrieval: recall, rank, join-complete, prune, evidence.

Step 05 of the optimization plan replaces "load every table" with an explicit
pipeline:

1. recall candidates (matched entities/metrics, question-term overlap, subject scope)
2. rank them (semantic match, question-term overlap, foreign-key distance)
3. complete the join graph (metric base entity, metric columns, resolved join
   paths and the foreign-key neighbours needed to reach requested dimensions)
4. prune columns (hidden columns first, then required columns, then budget)
5. return :class:`SchemaRetrievalResult` with per-table reasons, omitted
   tables/columns and a JSON-ready ``evidence`` payload.

The retriever performs no I/O; it only reads the policy-filtered schemas and the
already-loaded semantic model, so results are deterministic for a given input.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Iterable, Literal, Sequence

from pydantic import BaseModel, ConfigDict, Field

from queryforge.domain.semantic.model import SemanticModelLoader
from queryforge.domain.semantic.schemas import (
    MetricMatch,
    ResolvedJoinPath,
    SemanticModelContext,
)

if TYPE_CHECKING:  # pragma: no cover - typing only, avoids an import cycle
    from queryforge.core.schemas.models import TableSchema


RetrievalMode = Literal["semantic", "lexical_fallback", "passthrough"]

#: Reasons a table entered the candidate set, with their ranking weight.
RECALL_WEIGHTS: dict[str, float] = {
    "metric_base": 1.0,
    "semantic_entity": 0.9,
    "semantic_dimension": 0.8,
    "subject_scope": 0.7,
    "metric_join_path": 0.65,
    "join_path": 0.6,
    "fk_graph_path": 0.55,
    "fk_neighbour": 0.45,
    "question_terms": 0.3,
    "fallback_all": 0.1,
}

#: Table-name prefixes that carry no discriminating meaning.
GENERIC_TOKENS = frozenset(
    {
        "dim",
        "dims",
        "fact",
        "facts",
        "bridge",
        "stg",
        "stage",
        "staging",
        "tbl",
        "table",
        "raw",
        "src",
    }
)

#: Column tokens too generic to prove relevance on their own.
GENERIC_COLUMN_TOKENS = frozenset(
    {
        "id",
        "key",
        "pk",
        "fk",
        "code",
        "type",
        "flag",
        "num",
        "number",
        "no",
        "name",
        "value",
        "date",
        "time",
        "ts",
        "dt",
        "created",
        "updated",
        "at",
    }
)

QUESTION_STOPWORDS = frozenset(
    {
        "about",
        "after",
        "all",
        "and",
        "any",
        "are",
        "before",
        "between",
        "both",
        "by",
        "can",
        "did",
        "does",
        "each",
        "for",
        "from",
        "give",
        "has",
        "have",
        "how",
        "into",
        "its",
        "last",
        "list",
        "many",
        "more",
        "most",
        "much",
        "not",
        "of",
        "over",
        "per",
        "please",
        "show",
        "some",
        "than",
        "that",
        "the",
        "their",
        "them",
        "then",
        "there",
        "these",
        "this",
        "those",
        "top",
        "total",
        "was",
        "were",
        "what",
        "when",
        "where",
        "which",
        "who",
        "why",
        "will",
        "with",
        "year",
    }
)

_ENGLISH = re.compile(r"[a-z][a-z0-9]*")
_CJK = re.compile(r"[\u4e00-\u9fff]+")
_CAMEL_BOUNDARY = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")
_IDENTIFIER_COLUMN = re.compile(r"(_id|_key|_code|_fk|_pk|_ref|_uuid|_guid)$", re.IGNORECASE)


class TableRetrievalSelection(BaseModel):
    """One selected table with the reason it entered the prompt context."""

    model_config = ConfigDict(extra="forbid")

    table_name: str
    reason: str
    score: float = 0.0
    required: bool = False
    recalled_by: list[str] = Field(default_factory=list)
    columns: list[str] = Field(default_factory=list)
    omitted_columns: list[str] = Field(default_factory=list)

    def to_evidence(self, *, column_limit: int = 50) -> dict[str, Any]:
        return {
            "table_name": self.table_name,
            "reason": self.reason,
            "score": round(self.score, 4),
            "required": self.required,
            "recalled_by": list(self.recalled_by),
            "kept_columns": len(self.columns),
            "columns": self.columns[:column_limit],
            "omitted_columns": self.omitted_columns,
        }


@dataclass
class SchemaRetrievalResult:
    """Outcome of one retrieval pass (selected schemas + auditable evidence)."""

    mode: RetrievalMode = "passthrough"
    selected_tables: list["TableSchema"] = field(default_factory=list)
    selections: list[TableRetrievalSelection] = field(default_factory=list)
    omitted_tables: list[str] = field(default_factory=list)
    omitted_columns: dict[str, list[str]] = field(default_factory=dict)
    required_tables: list[str] = field(default_factory=list)
    evidence: dict[str, Any] = field(default_factory=dict)

    @property
    def selected_table_names(self) -> list[str]:
        return [schema.table_name for schema in self.selected_tables]

    def selection_by_table(self) -> dict[str, TableRetrievalSelection]:
        return {selection.table_name: selection for selection in self.selections}


class SchemaRetriever:
    """Recall, rank, join-complete and prune a policy-filtered schema list."""

    #: Mirrors ``GenSqlNode.MAX_TABLES_IN_PROMPT`` / ``MAX_COLUMNS_PER_TABLE``.
    DEFAULT_MAX_TABLES = 50
    DEFAULT_MAX_COLUMNS_PER_TABLE = 100

    #: Foreign-key hops used to rank borderline candidates (recall never relies
    #: on foreign keys alone; the join graph only *adds* tables it must reach).
    FK_RANKING_DISTANCE_LIMIT = 4
    #: Longest foreign-key path searched when closing the join graph.
    MAX_FK_PATH_LENGTH = 4

    def __init__(
        self,
        max_tables: int | None = None,
        max_columns_per_table: int | None = None,
    ) -> None:
        self.max_tables = self._positive(
            max_tables, self.DEFAULT_MAX_TABLES, "max_tables"
        )
        self.max_columns_per_table = self._positive(
            max_columns_per_table,
            self.DEFAULT_MAX_COLUMNS_PER_TABLE,
            "max_columns_per_table",
        )

    # ------------------------------------------------------------------ public

    def retrieve(
        self,
        schemas: Sequence["TableSchema"],
        question: str,
        *,
        semantic_model: SemanticModelContext | None = None,
        metric_matches: Sequence[MetricMatch] | None = None,
        metric_join_paths: Sequence[ResolvedJoinPath] | None = None,
        requested_dimensions: Sequence[str] | None = None,
        subject_tables: Sequence[str] | None = None,
        max_tables: int | None = None,
        max_columns_per_table: int | None = None,
    ) -> SchemaRetrievalResult:
        """Return the bounded schema selection for one question."""
        table_budget = self._positive(max_tables, self.max_tables, "max_tables")
        column_budget = self._positive(
            max_columns_per_table,
            self.max_columns_per_table,
            "max_columns_per_table",
        )
        schema_list = list(schemas)
        by_name = _unique_by_name(schema_list)

        if semantic_model is None:
            # Existing behaviour: no semantic layer means the full schema.
            return SchemaRetrievalResult(
                mode="passthrough",
                selected_tables=schema_list,
                selections=[],
                omitted_tables=[],
                omitted_columns={},
                required_tables=[schema.table_name for schema in schema_list],
                evidence={
                    "mode": "passthrough",
                    "reason": "no_semantic_model",
                    "budget": {
                        "max_tables": table_budget,
                        "max_columns_per_table": column_budget,
                    },
                    "candidate_tables": len(schema_list),
                    "selected_count": len(schema_list),
                    "selected_tables": [],
                    "omitted_tables": [],
                    "omitted_columns": {},
                    "required_tables": [schema.table_name for schema in schema_list],
                    "recall": {},
                    "degradation": [],
                },
            )

        model = semantic_model.model
        entities = {entity.name: entity for entity in model.entities}
        table_to_entity = _table_to_entity(entities.values())

        matches = list(metric_matches or [])
        if not matches:
            # ``SchemaLinkingNode`` runs before ``MetricSearchNode``; reuse the
            # same deterministic matcher so both nodes agree on metric scope.
            matches = SemanticModelLoader.match_metrics(model, question)
        matches = [match for match in matches if match.metric.entity in entities]

        scores: dict[str, float] = {}
        reasons: dict[str, list[str]] = {}
        recalled_by: dict[str, list[str]] = {}
        required: dict[str, list[str]] = {}
        required_columns: dict[str, list[str]] = {}
        degradation: list[str] = []
        overlap_cache: dict[str, float] = {}
        terms = QuestionTerms.from_question(question)

        def remember(
            table: str,
            kind: str,
            *,
            detail: str | None = None,
            is_required: bool = False,
        ) -> None:
            if table not in by_name:
                return
            recalled_by.setdefault(table, [])
            if kind not in recalled_by[table]:
                recalled_by[table].append(kind)
            reasons.setdefault(table, [])
            label = f"{kind}:{detail}" if detail else kind
            if label not in reasons[table]:
                reasons[table].append(label)
            weight = RECALL_WEIGHTS.get(kind, 0.1)
            if is_required:
                required.setdefault(table, [])
                if label not in required[table]:
                    required[table].append(label)
            base = scores.get(table, 0.0)
            if weight > base:
                scores[table] = weight

        # (a) recall: matched entities/metrics and requested dimensions.
        #
        # Foreign keys are *not* a recall reason: the join graph only *adds*
        # tables that a requested dimension or anchor actually needs (step c).
        for match in matches:
            entity = entities[match.metric.entity]
            remember(
                entity.table,
                "metric_base",
                detail=match.metric.name,
                is_required=True,
            )
        for match in semantic_model.matches:
            kind = "semantic_entity" if match.kind == "entity" else "semantic_dimension"
            remember(match.table, kind, detail=match.semantic_name)
            if match.column:
                _add_column(required_columns, match.table, match.column)

        # (a) recall: subject-tree tables.
        for table in subject_tables or []:
            remember(table, "subject_scope")

        # (a) recall: question-term overlap over table and column names.
        for schema in schema_list:
            overlap, hits = self._term_overlap(schema, terms)
            if overlap <= 0:
                continue
            overlap_cache[schema.table_name] = overlap
            remember(schema.table_name, "question_terms", detail=",".join(hits[:3]))

        graph = self._fk_graph(schema_list, semantic_model)
        distances = _bfs_distances(graph, sorted(scores))

        # (c) join-graph completion: required tables and columns.
        dimension_entities = self._dimension_entities(
            matches, requested_dimensions, semantic_model, table_to_entity, entities
        )
        metric_paths: list[ResolvedJoinPath] = list(metric_join_paths or [])
        anchors = {entity for match in matches for entity in [match.metric.entity]}
        anchors.update(
            entity
            for entity in (
                table_to_entity.get(match.table)
                for match in semantic_model.matches
                if match.kind == "entity"
            )
            if entity
        )
        resolved_path_names: list[str] = []
        fk_paths: list[list[str]] = []

        for match in matches:
            metric = match.metric
            entity = entities[metric.entity]
            base_table = entity.table
            _add_columns(
                required_columns,
                base_table,
                _expression_columns(
                    [metric.expression, *metric.default_filters], base_table
                ),
            )
            if metric.time_field:
                time_table, time_column = _split_reference(metric.time_field)
                _add_column(required_columns, time_table, time_column)
            _add_columns(required_columns, base_table, entity.effective_grain)
            for dimension_entity in sorted(dimension_entities):
                if dimension_entity == metric.entity or dimension_entity not in entities:
                    continue
                target_table = entities[dimension_entity].table
                path = SemanticModelLoader.resolve_join_path(
                    model, metric.entity, dimension_entity
                )
                if path is not None:
                    for table in path.tables:
                        remember(
                            table,
                            "join_path",
                            detail=path.name,
                            is_required=True,
                        )
                    _add_path_columns(required_columns, path)
                    if path.name not in resolved_path_names:
                        resolved_path_names.append(path.name)
                    continue
                fk_path = _shortest_path(
                    graph, base_table, target_table, self.MAX_FK_PATH_LENGTH
                )
                if fk_path:
                    for table in fk_path:
                        remember(
                            table,
                            "fk_graph_path",
                            detail=f"{base_table}->{target_table}",
                            is_required=True,
                        )
                    fk_paths.append(fk_path)
                else:
                    degradation.append(
                        f"no_join_path:{metric.entity}->{dimension_entity}"
                    )

        # explicit join paths resolved by the caller (MetricSearchNode output).
        for path in metric_paths:
            for table in path.tables:
                remember(
                    table,
                    "metric_join_path",
                    detail=path.name,
                    is_required=True,
                )
            _add_path_columns(required_columns, path)
            if path.name not in resolved_path_names:
                resolved_path_names.append(path.name)

        # connect the remaining semantic anchors so named entities can be joined.
        anchor_entities = sorted(anchors)
        if len(anchor_entities) > 1:
            reference = entities.get(anchor_entities[0])
            if reference is not None:
                for other in anchor_entities[1:]:
                    target = entities.get(other)
                    if target is None or target.table == reference.table:
                        continue
                    if _has_semantic_connection(model, reference.table, target.table):
                        continue
                    fk_path = _shortest_path(
                        graph, reference.table, target.table, self.MAX_FK_PATH_LENGTH
                    )
                    if fk_path:
                        for table in fk_path:
                            remember(
                                table,
                                "fk_graph_path",
                                detail=f"{reference.table}->{target.table}",
                                is_required=True,
                            )
                        fk_paths.append(fk_path)

        # join keys: every foreign key of every table we keep is a required column.
        for table in set(scores) | set(required):
            schema = by_name.get(table)
            if schema is None:
                continue
            for foreign_key in schema.foreign_keys:
                _add_column(required_columns, table, foreign_key.column)
                _add_column(required_columns, foreign_key.referenced_table, foreign_key.referenced_column)
            entity_name = table_to_entity.get(table)
            entity = entities.get(entity_name) if entity_name else None
            if entity is not None:
                _add_columns(required_columns, table, entity.effective_grain)

        # (b) rank: semantic weight, question overlap, then FK distance.
        for table in list(scores):
            distance = distances.get(table)
            if distance is None or distance > self.FK_RANKING_DISTANCE_LIMIT:
                proximity = 0.0
            else:
                proximity = 0.2 / (1.0 + distance)
            scores[table] = round(
                scores[table]
                + min(0.4, 0.1 * overlap_cache.get(table, 0.0))
                + proximity,
                6,
            )

        ranked = sorted(scores, key=lambda table: (-scores[table], table))
        required_names = sorted(required)
        has_semantic_evidence = any(
            kind
            in {
                "metric_base",
                "semantic_entity",
                "semantic_dimension",
                "subject_scope",
                "join_path",
                "metric_join_path",
                "fk_graph_path",
            }
            for kinds in recalled_by.values()
            for kind in kinds
        )
        mode: RetrievalMode = "semantic" if has_semantic_evidence else "lexical_fallback"

        selected_names = [table for table in required_names if table in by_name]
        for table in ranked:
            if len(selected_names) >= table_budget:
                break
            if table not in selected_names:
                selected_names.append(table)
        if mode == "lexical_fallback" and not selected_names:
            # No recall evidence at all: keep a deterministic, budgeted fallback
            # slice instead of sending an empty schema to generation.
            mode = "lexical_fallback"
            selected_names = [schema.table_name for schema in schema_list][:table_budget]
            for table in selected_names:
                remember(table, "fallback_all")
            degradation.append("no_question_or_semantic_hits")
        budget_exceeded = len(required_names) > table_budget
        if budget_exceeded:
            degradation.append("required_tables_exceed_max_tables")

        # order the final selection by rank for stable prompting.
        ordered: list[str] = []
        for table in ranked:
            if table in selected_names and table not in ordered:
                ordered.append(table)
        for table in selected_names:
            if table not in ordered:
                ordered.append(table)

        # (d) column pruning.
        hidden_refs = semantic_model.hidden_column_refs()
        selected_schemas: list["TableSchema"] = []
        selections: list[TableRetrievalSelection] = []
        omitted_columns: dict[str, list[str]] = {}
        for table in ordered:
            schema = by_name[table]
            kept, omitted_hidden, omitted_budget = self._prune_columns(
                schema,
                required=required_columns.get(table, []),
                hidden={column for ref_table, column in hidden_refs if ref_table == table},
                terms=terms,
                budget=column_budget,
            )
            selected_schemas.append(schema.model_copy(update={"columns": kept}))
            omitted_columns[table] = [*omitted_hidden, *omitted_budget]
            selections.append(
                TableRetrievalSelection(
                    table_name=table,
                    reason="; ".join(reasons.get(table, []) or ["ranked_candidate"]),
                    score=scores.get(table, 0.0),
                    required=table in required,
                    recalled_by=recalled_by.get(table, []),
                    columns=[column.name for column in kept],
                    omitted_columns=[*omitted_hidden, *omitted_budget],
                )
            )

        omitted_tables = sorted(set(by_name) - set(ordered))
        evidence = self._build_evidence(
            mode=mode,
            question_terms=terms,
            schema_list=schema_list,
            selections=selections,
            ordered=ordered,
            required_names=required_names,
            omitted_tables=omitted_tables,
            omitted_columns=omitted_columns,
            table_budget=table_budget,
            column_budget=column_budget,
            budget_exceeded=budget_exceeded,
            matches=matches,
            semantic_model=semantic_model,
            dimension_entities=sorted(dimension_entities),
            resolved_path_names=resolved_path_names,
            fk_paths=fk_paths,
            recalled_by=recalled_by,
            degradation=degradation,
            overlap_cache=overlap_cache,
        )
        return SchemaRetrievalResult(
            mode=mode,
            selected_tables=selected_schemas,
            selections=selections,
            omitted_tables=omitted_tables,
            omitted_columns=omitted_columns,
            required_tables=required_names,
            evidence=evidence,
        )

    # --------------------------------------------------------------- internals

    def _term_overlap(
        self, schema: "TableSchema", terms: "QuestionTerms"
    ) -> tuple[float, list[str]]:
        hits: list[str] = []
        table_tokens = {
            token
            for token in _tokens(schema.table_name)
            if token not in GENERIC_TOKENS and len(token) >= 3
        }
        table_matches = sorted(table_tokens & terms.english)
        hits.extend(table_matches)
        column_hits: list[str] = []
        for column in schema.columns:
            if _IDENTIFIER_COLUMN.search(column.name):
                # identifier/foreign-key columns never prove lexical relevance.
                continue
            column_tokens = {
                token
                for token in _tokens(column.name)
                if token not in GENERIC_COLUMN_TOKENS and len(token) >= 3
            }
            matched = sorted(column_tokens & terms.english)
            column_hits.extend(matched)
            hits.extend(matched)
        overlap = 2.0 * len(table_matches) + 0.5 * min(6, len(set(column_hits)))
        if terms.chinese_terms:
            cjk_hit = terms.contains_chinese(schema.table_name)
            if cjk_hit:
                hits.append(cjk_hit)
                overlap += 2.0
            for column in schema.columns:
                cjk_hit = terms.contains_chinese(column.name)
                if cjk_hit:
                    hits.append(cjk_hit)
                    overlap += 0.5
        return overlap, [hit for hit in dict.fromkeys(hits)]

    def _fk_graph(
        self,
        schemas: Sequence["TableSchema"],
        semantic_model: SemanticModelContext | None,
    ) -> dict[str, set[str]]:
        names = {schema.table_name for schema in schemas}
        graph: dict[str, set[str]] = {name: set() for name in names}
        for schema in schemas:
            for foreign_key in schema.foreign_keys:
                other = foreign_key.referenced_table
                if other in names and other != schema.table_name:
                    graph[schema.table_name].add(other)
                    graph[other].add(schema.table_name)
        if semantic_model is not None:
            for relationship in semantic_model.model.relationships:
                left = _split_reference(relationship.from_ref)[0]
                right = _split_reference(relationship.to_ref)[0]
                if left in names and right in names and left != right:
                    graph[left].add(right)
                    graph[right].add(left)
        return graph

    def _dimension_entities(
        self,
        matches: Sequence[MetricMatch],
        requested_dimensions: Sequence[str] | None,
        semantic_model: SemanticModelContext,
        table_to_entity: dict[str, str],
        entities: dict[str, Any],
    ) -> set[str]:
        dimension_entities: set[str] = set()
        for reference in requested_dimensions or []:
            entity_name = str(reference).split(".", 1)[0]
            if entity_name in entities:
                dimension_entities.add(entity_name)
        # When metrics matched, only dimensions those metrics are allowed to be
        # grouped by count as "requested" (the governed vocabulary).
        allowed_dimensions = {
            str(reference)
            for metric_match in matches
            for reference in metric_match.metric.allowed_dimensions
        }
        for match in semantic_model.matches:
            if match.kind != "dimension":
                continue
            entity_name = table_to_entity.get(match.table)
            if not entity_name:
                continue
            reference = f"{entity_name}.{match.semantic_name}"
            if allowed_dimensions and reference not in allowed_dimensions:
                continue
            dimension_entities.add(entity_name)
        return {name for name in dimension_entities if name in entities}

    def _prune_columns(
        self,
        schema: "TableSchema",
        *,
        required: Iterable[str],
        hidden: set[str],
        terms: "QuestionTerms",
        budget: int,
    ) -> tuple[list[Any], list[str], list[str]]:
        required_names = {name for name in required}
        omitted_hidden: list[str] = []
        available: list[Any] = []
        for column in schema.columns:
            if column.name in hidden:
                omitted_hidden.append(column.name)
                continue
            available.append(column)
        keep_required = [column for column in available if column.name in required_names]
        optional = [column for column in available if column.name not in required_names]
        remaining = max(0, budget - len(keep_required))
        scored = sorted(
            enumerate(optional),
            key=lambda item: (
                -_column_overlap(item[1].name, terms),
                item[0],
            ),
        )
        chosen_names = {column.name for column in keep_required}
        for _, column in scored[:remaining]:
            chosen_names.add(column.name)
        kept = [column for column in schema.columns if column.name in chosen_names]
        omitted_budget = [
            column.name
            for column in available
            if column.name not in chosen_names
        ]
        return kept, omitted_hidden, omitted_budget

    def _build_evidence(
        self,
        *,
        mode: RetrievalMode,
        question_terms: "QuestionTerms",
        schema_list: Sequence["TableSchema"],
        selections: Sequence[TableRetrievalSelection],
        ordered: Sequence[str],
        required_names: Sequence[str],
        omitted_tables: Sequence[str],
        omitted_columns: dict[str, list[str]],
        table_budget: int,
        column_budget: int,
        budget_exceeded: bool,
        matches: Sequence[MetricMatch],
        semantic_model: SemanticModelContext | None,
        dimension_entities: Sequence[str],
        resolved_path_names: Sequence[str],
        fk_paths: Sequence[Sequence[str]],
        recalled_by: dict[str, list[str]],
        degradation: Sequence[str],
        overlap_cache: dict[str, float],
    ) -> dict[str, Any]:
        recall_counts: dict[str, int] = {}
        for kinds in recalled_by.values():
            for kind in kinds:
                recall_counts[kind] = recall_counts.get(kind, 0) + 1
        return {
            "mode": mode,
            "step": "05",
            "budget": {
                "max_tables": table_budget,
                "max_columns_per_table": column_budget,
            },
            "candidate_tables": len(schema_list),
            "selected_count": len(ordered),
            "selected_tables": [
                selection.to_evidence() for selection in selections
            ],
            "selected_table_names": list(ordered),
            "required_tables": list(required_names),
            "omitted_tables": list(omitted_tables),
            "omitted_columns": {
                table: columns[:50] for table, columns in omitted_columns.items() if columns
            },
            "omitted_columns_count": sum(
                len(columns) for columns in omitted_columns.values()
            ),
            "metric_matches": [match.metric.name for match in matches],
            "dimension_entities": list(dimension_entities),
            "join_paths": list(resolved_path_names),
            "fk_completion_paths": [list(path) for path in fk_paths],
            "question_terms": {
                "english": sorted(question_terms.english)[:40],
                "chinese": list(question_terms.chinese_terms)[:20],
                "overlap_tables": sorted(overlap_cache),
            },
            "recall": recall_counts,
            "semantic_model": (
                semantic_model.model.name if semantic_model is not None else None
            ),
            "budget_respected": len(ordered) <= table_budget,
            "required_tables_exceed_budget": budget_exceeded,
            "degradation": list(dict.fromkeys(degradation)),
        }

    @staticmethod
    def _positive(value: int | None, default: int, label: str) -> int:
        candidate = default if value is None else value
        if not isinstance(candidate, int) or isinstance(candidate, bool):
            raise TypeError(f"{label} must be an integer")
        if candidate < 1:
            raise ValueError(f"{label} must be positive")
        return candidate


class QuestionTerms(BaseModel):
    """Normalized English/Chinese question terms used for lexical overlap."""

    model_config = ConfigDict(extra="forbid")

    english: set[str] = Field(default_factory=set)
    chinese_terms: list[str] = Field(default_factory=list)
    normalized: str = ""

    @classmethod
    def from_question(cls, question: str) -> "QuestionTerms":
        normalized = " ".join(
            re.findall(r"[\w]+", question.casefold(), flags=re.UNICODE)
        )
        english = {
            token
            for token in _ENGLISH.findall(question.casefold())
            if len(token) >= 3 and token not in QUESTION_STOPWORDS
        }
        chinese_terms: list[str] = []
        for sequence in _CJK.findall(question.casefold()):
            if sequence not in chinese_terms:
                chinese_terms.append(sequence)
            for size in (2, 3):
                for start in range(0, max(0, len(sequence) - size + 1)):
                    gram = sequence[start : start + size]
                    if gram not in chinese_terms:
                        chinese_terms.append(gram)
        return cls(
            english=english,
            chinese_terms=chinese_terms[:60],
            normalized=normalized,
        )

    def contains_chinese(self, text: str) -> str | None:
        lowered = text.casefold()
        for term in self.chinese_terms:
            if len(term) >= 2 and term in lowered:
                return term
        return None


# --------------------------------------------------------------------- helpers


def _unique_by_name(schemas: Sequence["TableSchema"]) -> dict[str, "TableSchema"]:
    by_name: dict[str, "TableSchema"] = {}
    for schema in schemas:
        by_name.setdefault(schema.table_name, schema)
    return by_name


def _table_to_entity(entities: Iterable[Any]) -> dict[str, str]:
    mapping: dict[str, str] = {}
    for entity in entities:
        mapping.setdefault(entity.table, entity.name)
    return mapping


def _tokens(name: str) -> list[str]:
    spaced = _CAMEL_BOUNDARY.sub("_", name)
    return [token for token in re.findall(r"[a-z0-9]+", spaced.casefold()) if token]


def _column_overlap(column_name: str, terms: QuestionTerms) -> float:
    tokens = {
        token
        for token in _tokens(column_name)
        if token not in GENERIC_COLUMN_TOKENS and len(token) >= 3
    }
    score = float(len(tokens & terms.english))
    if terms.contains_chinese(column_name):
        score += 1.0
    return score


def _split_reference(reference: str) -> tuple[str, str]:
    if "." not in reference:
        return reference, reference
    table, column = reference.split(".", 1)
    return table.strip().strip('"'), column.strip().strip('"')


def _expression_columns(expressions: Iterable[str], table: str) -> list[str]:
    columns: list[str] = []
    for expression in expressions:
        for reference_table, columns_found in _extract_references(expression).items():
            if reference_table != table:
                continue
            for column in columns_found:
                if column not in columns:
                    columns.append(column)
    return columns


def _extract_references(expression: str) -> dict[str, list[str]]:
    pattern = re.compile(
        r'\b([A-Za-z_][A-Za-z0-9_]*)\.(?:"([^"]+)"|([A-Za-z_][A-Za-z0-9_]*))'
    )
    references: dict[str, list[str]] = {}
    for match in pattern.finditer(expression):
        table = match.group(1)
        column = match.group(2) or match.group(3)
        references.setdefault(table, [])
        if column not in references[table]:
            references[table].append(column)
    return references


def _add_column(target: dict[str, list[str]], table: str, column: str | None) -> None:
    if not table or not column:
        return
    target.setdefault(table, [])
    if column not in target[table]:
        target[table].append(column)


def _add_columns(
    target: dict[str, list[str]], table: str, columns: Iterable[str]
) -> None:
    for column in columns:
        _add_column(target, table, column)


def _add_path_columns(
    target: dict[str, list[str]], path: ResolvedJoinPath
) -> None:
    for step in path.steps:
        _add_column(target, step.from_table, step.from_column)
        _add_column(target, step.to_table, step.to_column)


def _has_semantic_connection(
    model: Any, left_table: str, right_table: str
) -> bool:
    for relationship in model.relationships:
        if {_split_reference(relationship.from_ref)[0], _split_reference(relationship.to_ref)[0]} == {
            left_table,
            right_table,
        }:
            return True
    return False


def _bfs_distances(
    graph: dict[str, set[str]], seeds: Sequence[str]
) -> dict[str, int]:
    distances: dict[str, int] = {}
    queue: list[str] = []
    for seed in sorted(seeds):
        if seed in graph and seed not in distances:
            distances[seed] = 0
            queue.append(seed)
    index = 0
    while index < len(queue):
        current = queue[index]
        index += 1
        for neighbour in sorted(graph.get(current, ())):
            if neighbour not in distances:
                distances[neighbour] = distances[current] + 1
                queue.append(neighbour)
    return distances


def _shortest_path(
    graph: dict[str, set[str]],
    start: str,
    target: str,
    max_length: int,
) -> list[str]:
    if start not in graph or target not in graph:
        return []
    if start == target:
        return [start]
    queue: list[list[str]] = [[start]]
    visited = {start}
    while queue:
        path = queue.pop(0)
        if len(path) - 1 >= max_length:
            continue
        for neighbour in sorted(graph.get(path[-1], ())):
            if neighbour in visited:
                continue
            next_path = [*path, neighbour]
            if neighbour == target:
                return next_path
            visited.add(neighbour)
            queue.append(next_path)
    return []
