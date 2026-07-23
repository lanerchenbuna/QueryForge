"""Infer, merge, validate, and atomically publish SQLite semantic models."""

from __future__ import annotations

import json
import re
import sqlite3
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from queryforge.core.schemas.models import TableColumn, TableSchema
from queryforge.domain.semantic.contract_validator import SemanticContractValidator
from queryforge.domain.semantic.model import SemanticModelLoader
from queryforge.domain.semantic.schemas import (
    SemanticDimension,
    SemanticEntity,
    SemanticJoinPath,
    SemanticMetric,
    SemanticModel,
    SemanticRelationship,
)
from queryforge.infrastructure.db.sqlite_connector import SQLiteConnector


PII_COLUMN_PATTERN = re.compile(
    r"(^|_)(email|phone|mobile|address|password|secret|token|api_key|"
    r"ssn|national_id|card_number|birth_date|date_of_birth)($|_)",
    re.IGNORECASE,
)
MEASURE_PATTERN = re.compile(
    r"(amount|revenue|cost|price|quantity|units|seconds|minutes|hours|"
    r"duration|impressions|views|clicks|score)$",
    re.IGNORECASE,
)
TECHNICAL_PREFIXES = ("dim_", "fact_", "bridge_", "stg_", "raw_")
PROJECT_ROOT = Path(__file__).resolve().parents[3]


class SemanticBuildError(ValueError):
    """Raised when a semantic model cannot be inferred or published safely."""


@dataclass(frozen=True)
class SemanticBuildResult:
    """Files and validation state produced by one semantic build."""

    model: SemanticModel
    output_path: Path
    draft_path: Path
    report_path: Path
    published: bool
    contract_passed: bool
    review_items: tuple[str, ...]

    def summary(self) -> dict[str, Any]:
        return {
            "status": "published" if self.published else "draft",
            "model": self.model.name,
            "output_path": str(self.output_path),
            "draft_path": str(self.draft_path),
            "report_path": str(self.report_path),
            "entities": len(self.model.entities),
            "relationships": len(self.model.relationships),
            "join_paths": len(self.model.join_paths),
            "metrics": len(self.model.metrics),
            "contract_passed": self.contract_passed,
            "review_items": len(self.review_items),
        }


class _NoAliasDumper(yaml.SafeDumper):
    def ignore_aliases(self, data: Any) -> bool:
        return True


class SemanticModelBuilder:
    """Build a reviewable semantic layer from SQLite metadata and profiles.

    Physical metadata supplies high-confidence structure. Naming and lightweight
    profiling only create reviewable suggestions; curated definitions from an
    existing model remain authoritative during an incremental build.
    """

    def __init__(
        self,
        database_path: str | Path,
        *,
        owner: str = "data-platform",
        profile: bool = True,
    ) -> None:
        self.database_path = Path(database_path).expanduser().resolve()
        if not self.database_path.is_file():
            raise SemanticBuildError(
                f"SQLite database does not exist: {self.database_path}"
            )
        self.owner = owner.strip() or "data-platform"
        self.profile = profile
        self._evidence: list[dict[str, Any]] = []
        self._review_items: list[str] = []
        self._profiles: dict[str, dict[str, Any]] = {}

    def infer(
        self,
        *,
        name: str | None = None,
        description: str | None = None,
    ) -> SemanticModel:
        """Infer a complete strict model that can be loaded immediately."""

        schemas = self._schemas()
        if not schemas:
            raise SemanticBuildError("SQLite database contains no user tables")
        self._profiles = self._profile_tables(schemas) if self.profile else {}
        entities = [self._infer_entity(schema) for schema in schemas]
        relationships = self._infer_relationships(schemas, entities)
        join_paths = self._infer_join_paths(entities, relationships)
        metrics = self._infer_metrics(entities, relationships, join_paths)
        model_name = name or self._identifier(self.database_path.stem)
        return SemanticModel(
            version=1,
            name=model_name,
            description=description
            or (
                f"Inferred semantic layer for {self.database_path.name}. "
                "Review generated metrics and heuristic relationships before "
                "using them as canonical business definitions."
            ),
            entities=entities,
            relationships=relationships,
            join_paths=join_paths,
            metrics=metrics,
        )

    def build(
        self,
        output_path: str | Path,
        *,
        existing_model_path: str | Path | None = None,
        report_path: str | Path | None = None,
        name: str | None = None,
        description: str | None = None,
        publish: bool = True,
    ) -> SemanticBuildResult:
        """Infer, merge curated semantics, validate, and publish atomically."""

        output = Path(output_path).expanduser().resolve()
        output.parent.mkdir(parents=True, exist_ok=True)
        existing = self._load_existing(existing_model_path)
        inferred = self.infer(
            name=name or (existing.name if existing else None),
            description=description or (existing.description if existing else None),
        )
        model = self._merge(inferred, existing) if existing else inferred
        if name:
            model = model.model_copy(update={"name": name})
        if description:
            model = model.model_copy(update={"description": description})

        draft = output.with_suffix(f".draft{output.suffix or '.yml'}")
        report = (
            Path(report_path).expanduser().resolve()
            if report_path
            else output.with_suffix(".build.json")
        )
        draft.parent.mkdir(parents=True, exist_ok=True)
        report.parent.mkdir(parents=True, exist_ok=True)
        self._write_yaml(draft, model)

        schemas = self._schemas()
        context = SemanticModelLoader.load_and_validate(draft, schemas, "")
        contract = SemanticContractValidator.validate(model, self.database_path)
        published = bool(publish and contract.passed)
        report_payload = {
            "version": 1,
            "database": _portable_path(self.database_path),
            "model": model.name,
            "status": "published" if published else "draft",
            "source_precedence": [
                "existing curated semantic model",
                "SQLite primary keys and foreign keys",
                "SQLite schema metadata",
                "bounded column profiling",
                "naming heuristics",
            ],
            "coverage": {
                "entities": len(model.entities),
                "relationships": len(model.relationships),
                "join_paths": len(model.join_paths),
                "metrics": len(model.metrics),
                "tables": len(schemas),
            },
            "contract": contract.summary(),
            "review_items": self._deduplicated_review_items(),
            "inference_evidence": self._evidence,
            "table_profiles": self._profiles,
            "existing_model": (
                _portable_path(Path(existing_model_path).expanduser().resolve())
                if existing_model_path
                else None
            ),
            "draft_path": _portable_path(draft),
            "output_path": _portable_path(output),
        }
        report.write_text(
            json.dumps(report_payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        if published:
            draft.replace(output)
        return SemanticBuildResult(
            model=context.model,
            output_path=output,
            draft_path=draft,
            report_path=report,
            published=published,
            contract_passed=contract.passed,
            review_items=tuple(self._deduplicated_review_items()),
        )

    def _schemas(self) -> list[TableSchema]:
        with SQLiteConnector(str(self.database_path)) as connector:
            return [
                connector.describe_table(table) for table in connector.list_tables()
            ]

    def _profile_tables(
        self, schemas: list[TableSchema]
    ) -> dict[str, dict[str, Any]]:
        profiles: dict[str, dict[str, Any]] = {}
        uri = f"{self.database_path.as_uri()}?mode=ro"
        with sqlite3.connect(uri, uri=True) as connection:
            connection.execute("PRAGMA query_only = ON")
            for schema in schemas:
                table = self._quote(schema.table_name)
                null_terms = ", ".join(
                    f"SUM(CASE WHEN {self._quote(column.name)} IS NULL THEN 1 ELSE 0 END)"
                    for column in schema.columns
                )
                query = f"SELECT COUNT(*)"
                if null_terms:
                    query += ", " + null_terms
                row = connection.execute(f"{query} FROM {table}").fetchone() or (0,)
                row_count = int(row[0])
                null_rates = {
                    column.name: (
                        round(int(row[index + 1] or 0) / row_count, 6)
                        if row_count
                        else 0.0
                    )
                    for index, column in enumerate(schema.columns)
                }
                profiles[schema.table_name] = {
                    "row_count": row_count,
                    "null_rates": null_rates,
                }
        return profiles

    def _infer_entity(self, schema: TableSchema) -> SemanticEntity:
        entity_name = self._entity_name(schema.table_name)
        primary_key = [column.name for column in schema.columns if column.primary_key]
        grain = list(primary_key)
        confidence = 1.0 if primary_key else 0.55
        reason = "SQLite primary key"
        if not grain:
            grain = self._infer_grain(schema)
            reason = "unique identifier naming/profile heuristic"
            if grain:
                self._review_items.append(
                    f"Confirm inferred grain {schema.table_name}.{grain[0]}."
                )
            else:
                self._review_items.append(
                    f"Declare a grain for {schema.table_name}; no reliable key was found."
                )
        hidden = [
            column.name
            for column in schema.columns
            if PII_COLUMN_PATTERN.search(column.name)
        ]
        foreign_key_columns = {item.column for item in schema.foreign_keys}
        dimensions: list[SemanticDimension] = []
        for column in schema.columns:
            if (
                column.name in hidden
                or column.name in primary_key
                or column.name in foreign_key_columns
            ):
                continue
            rules = []
            if column.name.endswith(("_flag", "_indicator")):
                rules = [{"rule": "range", "minimum": 0, "maximum": 1}]
            dimensions.append(
                SemanticDimension(
                    name=self._dimension_name(column.name),
                    column=column.name,
                    description=self._humanize(column.name),
                    synonyms=self._synonyms(column.name),
                    owner=self.owner,
                    sensitivity="internal",
                    quality_rules=rules,
                )
            )
        entity_type = (
            "fact"
            if schema.table_name.startswith("fact_")
            or self._looks_like_event_table(schema)
            else "dimension"
        )
        if entity_type == "fact" and not grain:
            entity_type = "dimension"
            self._review_items.append(
                f"{schema.table_name} looks event-like but remains a dimension until "
                "its grain is declared."
            )
        sensitivity = "confidential" if hidden else "internal"
        self._evidence.append(
            {
                "subject": f"entity:{entity_name}",
                "decision": {
                    "table": schema.table_name,
                    "entity_type": entity_type,
                    "grain": grain,
                    "hidden_columns": hidden,
                },
                "confidence": confidence,
                "source": reason,
            }
        )
        return SemanticEntity(
            name=entity_name,
            table=schema.table_name,
            description=f"Semantic entity inferred from {schema.table_name}.",
            entity_type=entity_type,
            synonyms=self._synonyms(entity_name),
            primary_key=primary_key,
            grain=grain,
            hidden_columns=hidden,
            expected_columns=[column.name for column in schema.columns],
            allow_additive_columns=True,
            dimensions=dimensions,
            owner=self.owner,
            sensitivity=sensitivity,
        )

    def _infer_grain(self, schema: TableSchema) -> list[str]:
        candidates = [
            column.name
            for column in schema.columns
            if column.name in {"id", f"{self._entity_name(schema.table_name)}_id"}
            or column.name.endswith("_id")
        ]
        if not candidates or not self.profile:
            return candidates[:1]
        row_count = self._profiles.get(schema.table_name, {}).get("row_count", 0)
        if row_count == 0:
            return candidates[:1]
        uri = f"{self.database_path.as_uri()}?mode=ro"
        with sqlite3.connect(uri, uri=True) as connection:
            connection.execute("PRAGMA query_only = ON")
            for candidate in candidates:
                distinct, non_null = connection.execute(
                    f"SELECT COUNT(DISTINCT {self._quote(candidate)}), "
                    f"COUNT({self._quote(candidate)}) "
                    f"FROM {self._quote(schema.table_name)}"
                ).fetchone()
                if int(distinct) == row_count and int(non_null) == row_count:
                    return [candidate]
        return []

    def _infer_relationships(
        self,
        schemas: list[TableSchema],
        entities: list[SemanticEntity],
    ) -> list[SemanticRelationship]:
        schemas_by_table = {schema.table_name: schema for schema in schemas}
        entities_by_table = {entity.table: entity for entity in entities}
        relationships: list[SemanticRelationship] = []
        seen_refs: set[tuple[str, str]] = set()
        for schema in schemas:
            for foreign_key in schema.foreign_keys:
                from_ref = f"{schema.table_name}.{foreign_key.column}"
                to_ref = (
                    f"{foreign_key.referenced_table}."
                    f"{foreign_key.referenced_column}"
                )
                relationship_type = self._relationship_type(
                    entities_by_table[schema.table_name],
                    foreign_key.column,
                )
                relationship = SemanticRelationship(
                    name=self._relationship_name(
                        schema.table_name,
                        foreign_key.column,
                        foreign_key.referenced_table,
                    ),
                    **{"from": from_ref, "to": to_ref},
                    relationship_type=relationship_type,
                    cardinality_contract={
                        "source": relationship_type.split("_to_")[0],
                        "target": relationship_type.split("_to_")[1],
                        "enforcement": "physical_fk",
                    },
                    description="Inferred from a SQLite foreign-key constraint.",
                )
                relationships.append(relationship)
                seen_refs.add((from_ref, to_ref))
                self._evidence.append(
                    {
                        "subject": f"relationship:{relationship.name}",
                        "decision": {
                            "from": from_ref,
                            "to": to_ref,
                            "cardinality": relationship_type,
                        },
                        "confidence": 1.0,
                        "source": "SQLite foreign key",
                    }
                )

        target_keys: dict[str, tuple[str, str]] = {}
        for entity in entities:
            if len(entity.effective_grain) == 1:
                target_keys[entity.effective_grain[0]] = (
                    entity.table,
                    entity.effective_grain[0],
                )
        for schema in schemas:
            for column in schema.columns:
                if not column.name.endswith(("_id", "_key")):
                    continue
                target = target_keys.get(column.name)
                if target is None or target[0] == schema.table_name:
                    continue
                from_ref = f"{schema.table_name}.{column.name}"
                to_ref = f"{target[0]}.{target[1]}"
                if (from_ref, to_ref) in seen_refs:
                    continue
                relationship = SemanticRelationship(
                    name=self._relationship_name(
                        schema.table_name, column.name, target[0]
                    ),
                    **{"from": from_ref, "to": to_ref},
                    relationship_type="many_to_one",
                    cardinality_contract={
                        "source": "many",
                        "target": "one",
                        "enforcement": "semantic",
                    },
                    description=(
                        "Suggested from matching key names; confirm before treating "
                        "as a canonical join."
                    ),
                )
                relationships.append(relationship)
                seen_refs.add((from_ref, to_ref))
                self._review_items.append(
                    f"Confirm heuristic relationship {from_ref} -> {to_ref}."
                )
                self._evidence.append(
                    {
                        "subject": f"relationship:{relationship.name}",
                        "decision": {"from": from_ref, "to": to_ref},
                        "confidence": 0.6,
                        "source": "matching key-name heuristic",
                    }
                )
        return self._unique_names(relationships)

    def _infer_join_paths(
        self,
        entities: list[SemanticEntity],
        relationships: list[SemanticRelationship],
    ) -> list[SemanticJoinPath]:
        by_table = {entity.table: entity for entity in entities}
        outgoing: dict[str, list[tuple[str, SemanticRelationship]]] = {}
        for relationship in relationships:
            from_table = relationship.from_ref.split(".", 1)[0]
            to_table = relationship.to_ref.split(".", 1)[0]
            if relationship.relationship_type not in {"many_to_one", "one_to_one"}:
                continue
            outgoing.setdefault(from_table, []).append((to_table, relationship))
        paths: list[SemanticJoinPath] = []
        seen_pairs: set[tuple[str, str]] = set()
        for source in entities:
            if source.entity_type != "fact":
                continue
            queue = deque([(source.table, [], {source.table})])
            while queue:
                table, steps, visited = queue.popleft()
                if len(steps) >= 3:
                    continue
                for next_table, relationship in sorted(
                    outgoing.get(table, []), key=lambda item: item[1].name
                ):
                    if next_table in visited or next_table not in by_table:
                        continue
                    next_steps = [*steps, relationship.name]
                    target = by_table[next_table]
                    pair = (source.name, target.name)
                    if len(next_steps) >= 2 and pair not in seen_pairs:
                        paths.append(
                            SemanticJoinPath(
                                name=f"{source.name}_to_{target.name}_inferred",
                                from_entity=source.name,
                                to_entity=target.name,
                                relationships=next_steps,
                                description=(
                                    "Automatically inferred safe many-to-one path."
                                ),
                                owner=self.owner,
                            )
                        )
                        seen_pairs.add(pair)
                    queue.append(
                        (next_table, next_steps, {*visited, next_table})
                    )
        return paths

    def _infer_metrics(
        self,
        entities: list[SemanticEntity],
        relationships: list[SemanticRelationship],
        join_paths: list[SemanticJoinPath],
    ) -> list[SemanticMetric]:
        entities_by_name = {entity.name: entity for entity in entities}
        direct_targets: dict[str, set[str]] = {}
        table_to_entity = {entity.table: entity.name for entity in entities}
        for relationship in relationships:
            if relationship.relationship_type not in {"many_to_one", "one_to_one"}:
                continue
            source_table = relationship.from_ref.split(".", 1)[0]
            target_table = relationship.to_ref.split(".", 1)[0]
            direct_targets.setdefault(table_to_entity[source_table], set()).add(
                table_to_entity[target_table]
            )
        path_targets: dict[str, set[str]] = {}
        for path in join_paths:
            path_targets.setdefault(path.from_entity, set()).add(path.to_entity)

        metrics: list[SemanticMetric] = []
        schemas = {schema.table_name: schema for schema in self._schemas()}
        for entity in entities:
            if entity.entity_type != "fact":
                continue
            allowed_dimensions = [
                f"{entity.name}.{dimension.name}" for dimension in entity.dimensions
            ]
            targets = sorted(
                direct_targets.get(entity.name, set())
                | path_targets.get(entity.name, set())
            )
            for target_name in targets:
                target = entities_by_name[target_name]
                allowed_dimensions.extend(
                    f"{target.name}.{dimension.name}"
                    for dimension in target.dimensions[:8]
                )
            time_field = self._time_field(entity)
            count_expression = (
                f"COUNT(DISTINCT {entity.table}.{entity.effective_grain[0]})"
                if len(entity.effective_grain) == 1
                else f"COUNT({entity.table}.{entity.effective_grain[0]})"
            )
            metrics.append(
                SemanticMetric(
                    name=f"{entity.name}_count",
                    description=f"Count of {self._humanize(entity.name)} records.",
                    entity=entity.name,
                    aggregation="count",
                    expression=count_expression,
                    synonyms=[
                        f"{self._humanize(entity.name)} count",
                        f"number of {self._humanize(entity.name)}",
                    ],
                    allowed_dimensions=allowed_dimensions,
                    time_field=time_field,
                    owner=self.owner,
                )
            )
            self._review_items.append(
                f"Confirm inferred metric {entity.name}_count and its business grain."
            )
            for column in schemas[entity.table].columns:
                if not self._numeric(column) or not MEASURE_PATTERN.search(column.name):
                    continue
                metric_name = self._identifier(f"total_{column.name}")
                metrics.append(
                    SemanticMetric(
                        name=metric_name,
                        description=(
                            f"Sum of {entity.table}.{column.name}; confirm the "
                            "business definition and exclusions."
                        ),
                        entity=entity.name,
                        aggregation="sum",
                        expression=f"SUM({entity.table}.{column.name})",
                        synonyms=[self._humanize(metric_name)],
                        allowed_dimensions=allowed_dimensions,
                        time_field=time_field,
                        owner=self.owner,
                    )
                )
                self._review_items.append(
                    f"Confirm inferred additive metric {metric_name}."
                )
            for column in schemas[entity.table].columns:
                if not column.name.endswith(("_flag", "_indicator")):
                    continue
                stem = re.sub(r"_(flag|indicator)$", "", column.name)
                metric_name = self._identifier(f"{stem}_rate")
                metrics.append(
                    SemanticMetric(
                        name=metric_name,
                        description=(
                            f"Share of {entity.name} rows where {column.name} is 1."
                        ),
                        entity=entity.name,
                        aggregation="ratio",
                        expression=(
                            f"CAST(SUM({entity.table}.{column.name}) AS REAL) / "
                            "NULLIF(COUNT(*), 0)"
                        ),
                        synonyms=[self._humanize(metric_name)],
                        allowed_dimensions=allowed_dimensions,
                        time_field=time_field,
                        owner=self.owner,
                    )
                )
                self._review_items.append(
                    f"Confirm inferred ratio metric {metric_name}."
                )
        return self._unique_names(metrics)

    def _merge(
        self, inferred: SemanticModel, existing: SemanticModel
    ) -> SemanticModel:
        inferred_by_table = {entity.table: entity for entity in inferred.entities}
        existing_tables = {entity.table for entity in existing.entities}
        entities: list[SemanticEntity] = []
        for existing_entity in existing.entities:
            proposal = inferred_by_table.get(existing_entity.table)
            if proposal is None:
                entities.append(existing_entity)
                continue
            dimensions = list(existing_entity.dimensions)
            previous_columns = set(existing_entity.expected_columns)
            new_columns = set(proposal.expected_columns) - previous_columns
            if previous_columns and new_columns:
                known_columns = {dimension.column for dimension in dimensions}
                dimensions.extend(
                    dimension
                    for dimension in proposal.dimensions
                    if dimension.column in new_columns
                    and dimension.column not in known_columns
                    and dimension.column not in existing_entity.hidden_columns
                )
                self._review_items.append(
                    f"Review new columns on {existing_entity.table}: "
                    + ", ".join(sorted(new_columns))
                    + "."
                )
            entities.append(
                existing_entity.model_copy(
                    update={
                        "dimensions": dimensions,
                        "expected_columns": sorted(
                            set(existing_entity.expected_columns)
                            | set(proposal.expected_columns)
                        ),
                        "hidden_columns": sorted(
                            set(existing_entity.hidden_columns)
                            | set(proposal.hidden_columns)
                        ),
                    }
                )
            )
        entities.extend(
            entity
            for entity in inferred.entities
            if entity.table not in existing_tables
        )

        relationships = list(existing.relationships)
        known_refs = {
            (relationship.from_ref, relationship.to_ref)
            for relationship in relationships
        }
        relationships.extend(
            relationship
            for relationship in inferred.relationships
            if (relationship.from_ref, relationship.to_ref) not in known_refs
        )
        existing_entity_names = {entity.name for entity in existing.entities}
        join_paths = list(existing.join_paths)
        known_path_pairs = {
            (path.from_entity, path.to_entity) for path in join_paths
        }
        join_paths.extend(
            path
            for path in inferred.join_paths
            if path.from_entity not in existing_entity_names
            and (path.from_entity, path.to_entity) not in known_path_pairs
        )
        metrics = list(existing.metrics)
        known_metric_names = {metric.name for metric in metrics}
        metrics.extend(
            metric
            for metric in inferred.metrics
            if metric.entity not in existing_entity_names
            and metric.name not in known_metric_names
        )
        self._review_items = [
            item
            for item in self._review_items
            if not (
                item.startswith("Confirm inferred metric ")
                or item.startswith("Confirm inferred additive metric ")
                or item.startswith("Confirm inferred ratio metric ")
                or any(
                    item.startswith(f"Confirm inferred grain {entity.table}.")
                    for entity in existing.entities
                )
            )
        ]
        for metric in metrics:
            if metric.entity not in existing_entity_names:
                self._review_items.append(
                    f"Confirm inferred metric {metric.name} and its business definition."
                )
        self._evidence.insert(
            0,
            {
                "subject": "merge",
                "decision": (
                    "Preserved curated entities, relationships, join paths, and "
                    "metrics; appended newly inferred schema coverage."
                ),
                "confidence": 1.0,
                "source": "existing semantic model",
            },
        )
        return SemanticModel(
            version=max(inferred.version, existing.version),
            name=existing.name,
            description=existing.description or inferred.description,
            entities=entities,
            relationships=self._unique_names(relationships),
            join_paths=self._unique_names(join_paths),
            metrics=self._unique_names(metrics),
        )

    def _load_existing(
        self, existing_model_path: str | Path | None
    ) -> SemanticModel | None:
        if not existing_model_path:
            return None
        path = Path(existing_model_path).expanduser().resolve()
        if not path.is_file():
            raise SemanticBuildError(f"Existing semantic model does not exist: {path}")
        try:
            payload = yaml.safe_load(path.read_text(encoding="utf-8"))
            return SemanticModel.model_validate(payload)
        except Exception as exc:
            raise SemanticBuildError(
                f"Existing semantic model could not be loaded: {exc}"
            ) from exc

    def _write_yaml(self, path: Path, model: SemanticModel) -> None:
        payload = model.model_dump(by_alias=True, exclude_none=True)
        path.write_text(
            yaml.dump(
                payload,
                Dumper=_NoAliasDumper,
                sort_keys=False,
                allow_unicode=True,
                width=100,
            ),
            encoding="utf-8",
        )

    def _relationship_type(
        self, entity: SemanticEntity, source_column: str
    ) -> str:
        return (
            "one_to_one"
            if source_column in entity.effective_grain
            and len(entity.effective_grain) == 1
            else "many_to_one"
        )

    @staticmethod
    def _looks_like_event_table(schema: TableSchema) -> bool:
        name = schema.table_name.lower()
        return any(
            token in name
            for token in ("event", "session", "order", "rating", "transaction")
        ) and any(column.name.endswith("_id") for column in schema.columns)

    @staticmethod
    def _numeric(column: TableColumn) -> bool:
        data_type = column.data_type.upper()
        return any(
            token in data_type
            for token in ("INT", "REAL", "NUM", "DEC", "DOUBLE", "FLOAT")
        )

    @staticmethod
    def _time_field(entity: SemanticEntity) -> str | None:
        candidates = [
            dimension.column
            for dimension in entity.dimensions
            if any(
                token in dimension.column.lower()
                for token in ("date", "time", "timestamp", "created_at", "updated_at")
            )
        ]
        if not candidates:
            candidates = [
                column
                for column in entity.expected_columns
                if any(
                    token in column.lower()
                    for token in ("date", "time", "timestamp", "created_at", "updated_at")
                )
            ]
        return f"{entity.table}.{candidates[0]}" if candidates else None

    @staticmethod
    def _entity_name(table: str) -> str:
        value = table
        for prefix in TECHNICAL_PREFIXES:
            if value.startswith(prefix):
                value = value[len(prefix) :]
                break
        return SemanticModelBuilder._identifier(value)

    @staticmethod
    def _dimension_name(column: str) -> str:
        return SemanticModelBuilder._identifier(column)

    @staticmethod
    def _identifier(value: str) -> str:
        normalized = re.sub(r"[^a-zA-Z0-9]+", "_", value.strip()).strip("_").lower()
        return normalized or "semantic_model"

    @staticmethod
    def _humanize(value: str) -> str:
        return re.sub(r"[_\s]+", " ", value).strip()

    @classmethod
    def _synonyms(cls, value: str) -> list[str]:
        human = cls._humanize(value)
        return [human] if human and human != value else []

    @classmethod
    def _relationship_name(
        cls, source_table: str, source_column: str, target_table: str
    ) -> str:
        role = re.sub(r"(_id|_key)$", "", source_column)
        return cls._identifier(
            f"{cls._entity_name(source_table)}_to_{role or cls._entity_name(target_table)}"
        )

    @staticmethod
    def _quote(value: str) -> str:
        return '"' + value.replace('"', '""') + '"'

    @staticmethod
    def _unique_names(items: list[Any]) -> list[Any]:
        result = []
        counts: dict[str, int] = {}
        for item in items:
            base = item.name
            count = counts.get(base, 0)
            counts[base] = count + 1
            if count:
                item = item.model_copy(update={"name": f"{base}_{count + 1}"})
            result.append(item)
        return result

    def _deduplicated_review_items(self) -> list[str]:
        return list(dict.fromkeys(self._review_items))


def _portable_path(path: Path) -> str:
    resolved = path.expanduser().resolve()
    try:
        return str(resolved.relative_to(PROJECT_ROOT))
    except ValueError:
        return str(resolved)
