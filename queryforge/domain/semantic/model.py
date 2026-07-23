"""Load and validate a small YAML semantic model against physical SQLite schema."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import ValidationError

from queryforge.domain.semantic.schemas import (
    CardinalityContract,
    ContractQualityRule,
    MetricMatch,
    OperationalContract,
    ResolvedJoinPath,
    ResolvedJoinStep,
    SemanticDimension,
    SemanticEntity,
    SemanticJoinPath,
    SemanticMatch,
    SemanticMetric,
    SemanticModel,
    SemanticModelContext,
    SemanticRelationship,
    StrictSemanticModel,
)


class SemanticModelError(ValueError):
    """Raised when semantic metadata is invalid or contradicts physical schema."""


class SemanticModelLoader:
    """Strict loader whose physical-schema validation runs before model generation."""

    @classmethod
    def load_and_validate(
        cls,
        path: str | Path,
        schemas: list[Any],
        question: str,
    ) -> SemanticModelContext:
        semantic_path = Path(path).expanduser().resolve()
        if semantic_path.suffix.lower() not in {".yml", ".yaml"}:
            raise SemanticModelError(
                f"Semantic model must be a .yml or .yaml file: {semantic_path}"
            )
        if not semantic_path.is_file():
            raise SemanticModelError(f"Semantic model does not exist: {semantic_path}")
        try:
            payload = yaml.safe_load(semantic_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, yaml.YAMLError) as exc:
            raise SemanticModelError(
                f"Could not read semantic model {semantic_path}: {exc}"
            ) from exc
        try:
            model = SemanticModel.model_validate(payload)
        except ValidationError as exc:
            raise SemanticModelError(
                f"Invalid semantic model {semantic_path}: {exc}"
            ) from exc

        cls.validate_physical_references(model, schemas)
        return SemanticModelContext(
            source_path=str(semantic_path),
            model=model,
            matches=cls.match_question(model, question),
        )

    @classmethod
    def validate_physical_references(
        cls, model: SemanticModel, schemas: list[Any]
    ) -> None:
        schemas_by_table = {schema.table_name: schema for schema in schemas}
        physical = {
            schema.table_name: {column.name for column in schema.columns}
            for schema in schemas
        }
        errors: list[str] = []
        entity_names: set[str] = set()
        entity_tables: set[str] = set()
        for entity in model.entities:
            if entity.name in entity_names:
                errors.append(f"duplicate entity name {entity.name!r}")
            entity_names.add(entity.name)
            if entity.table in entity_tables:
                errors.append(f"duplicate entity table {entity.table!r}")
            entity_tables.add(entity.table)
            if entity.table not in physical:
                errors.append(
                    f"entity {entity.name!r} references unknown table {entity.table!r}"
                )
                continue
            columns = physical[entity.table]
            cls._validate_columns(
                errors, entity.table, "primary_key", entity.primary_key, columns
            )
            cls._validate_columns(
                errors, entity.table, "grain", entity.grain, columns
            )
            if entity.entity_type == "fact" and not entity.effective_grain:
                errors.append(
                    f"fact entity {entity.name!r} must declare grain or primary_key"
                )
            cls._validate_columns(
                errors, entity.table, "hidden_columns", entity.hidden_columns, columns
            )
            dimension_names: set[str] = set()
            for dimension in entity.dimensions:
                if dimension.name in dimension_names:
                    errors.append(
                        f"entity {entity.name!r} has duplicate dimension "
                        f"{dimension.name!r}"
                    )
                dimension_names.add(dimension.name)
                if dimension.column not in columns:
                    errors.append(
                        f"dimension {entity.name}.{dimension.name} references unknown "
                        f"column {entity.table}.{dimension.column}"
                    )
                if dimension.column in entity.hidden_columns:
                    errors.append(
                        f"dimension {entity.name}.{dimension.name} references hidden "
                        f"column {entity.table}.{dimension.column}"
                    )

        entities_by_name = {entity.name: entity for entity in model.entities}
        entities_by_table = {entity.table: entity for entity in model.entities}
        relationship_names: set[str] = set()
        for relationship in model.relationships:
            if relationship.name in relationship_names:
                errors.append(f"duplicate relationship name {relationship.name!r}")
            relationship_names.add(relationship.name)
            for label, reference in (
                ("from", relationship.from_ref),
                ("to", relationship.to_ref),
            ):
                parsed = cls._parse_column_reference(reference)
                if parsed is None:
                    errors.append(
                        f"relationship {relationship.name!r} has invalid {label} "
                        f"reference {reference!r}; expected table.column"
                    )
                    continue
                table, column = parsed
                if table not in entity_tables:
                    errors.append(
                        f"relationship {relationship.name!r} {label} table {table!r} "
                        "is not declared as an entity"
                    )
                if table not in physical:
                    errors.append(
                        f"relationship {relationship.name!r} references unknown table "
                        f"{table!r}"
                    )
                elif column not in physical[table]:
                    errors.append(
                        f"relationship {relationship.name!r} references unknown column "
                        f"{table}.{column}"
                    )

            from_ref = cls._parse_column_reference(relationship.from_ref)
            to_ref = cls._parse_column_reference(relationship.to_ref)
            if not from_ref or not to_ref:
                continue
            from_table, from_column = from_ref
            to_table, to_column = to_ref
            from_entity = entities_by_table.get(from_table)
            to_entity = entities_by_table.get(to_table)
            contract = relationship.effective_contract
            if contract.source == "one" and from_entity is not None:
                if from_column not in from_entity.effective_grain:
                    errors.append(
                        f"relationship {relationship.name!r} declares source='one' "
                        f"but {from_table}.{from_column} is not in source grain "
                        f"{from_entity.effective_grain!r}"
                    )
            if contract.target == "one" and to_entity is not None:
                if to_column not in to_entity.effective_grain:
                    errors.append(
                        f"relationship {relationship.name!r} declares target='one' "
                        f"but {to_table}.{to_column} is not in target grain "
                        f"{to_entity.effective_grain!r}"
                    )
            if contract.enforcement == "physical_fk":
                source_schema = schemas_by_table.get(from_table)
                physical_fk = bool(
                    source_schema
                    and any(
                        foreign_key.column == from_column
                        and foreign_key.referenced_table == to_table
                        and foreign_key.referenced_column == to_column
                        for foreign_key in source_schema.foreign_keys
                    )
                )
                if not physical_fk:
                    errors.append(
                        f"relationship {relationship.name!r} requires physical FK "
                        f"{from_table}.{from_column} -> {to_table}.{to_column}, but "
                        "SQLite does not declare it"
                    )

        join_path_names: set[str] = set()
        for join_path in model.join_paths:
            if join_path.name in join_path_names:
                errors.append(f"duplicate join path name {join_path.name!r}")
            join_path_names.add(join_path.name)
            if join_path.from_entity not in entities_by_name:
                errors.append(
                    f"join path {join_path.name!r} references unknown from_entity "
                    f"{join_path.from_entity!r}"
                )
            if join_path.to_entity not in entities_by_name:
                errors.append(
                    f"join path {join_path.name!r} references unknown to_entity "
                    f"{join_path.to_entity!r}"
                )
            unknown_relationships = [
                name
                for name in join_path.relationships
                if name not in relationship_names
            ]
            if unknown_relationships:
                errors.append(
                    f"join path {join_path.name!r} references unknown relationships "
                    f"{unknown_relationships!r}"
                )
                continue
            resolved = cls._resolve_relationship_sequence(
                model,
                join_path.from_entity,
                join_path.to_entity,
                join_path.relationships,
                name=join_path.name,
                explicit=True,
            )
            if resolved is None:
                errors.append(
                    f"join path {join_path.name!r} is not continuous from "
                    f"{join_path.from_entity!r} to {join_path.to_entity!r}"
                )

        metric_names: set[str] = set()
        for metric in model.metrics:
            if metric.name in metric_names:
                errors.append(f"duplicate metric name {metric.name!r}")
            metric_names.add(metric.name)
            entity = entities_by_name.get(metric.entity)
            if entity is None:
                errors.append(
                    f"metric {metric.name!r} references unknown entity {metric.entity!r}"
                )
                continue
            expected_function = {"count": "COUNT", "sum": "SUM"}.get(
                metric.aggregation
            )
            if expected_function and not re.search(
                rf"\b{expected_function}\s*\(", metric.expression, re.IGNORECASE
            ):
                errors.append(
                    f"metric {metric.name!r} aggregation {metric.aggregation!r} "
                    f"requires {expected_function}(...) in expression"
                )
            if metric.aggregation == "ratio" and "/" not in metric.expression:
                errors.append(
                    f"metric {metric.name!r} aggregation 'ratio' requires division "
                    "in expression"
                )
            expressions = [metric.expression, *metric.default_filters]
            for expression in expressions:
                references = cls._extract_physical_references(expression)
                if not references:
                    errors.append(
                        f"metric {metric.name!r} expression/filter must use at least "
                        "one qualified table.column reference"
                    )
                for table, column in references:
                    if table not in physical:
                        errors.append(
                            f"metric {metric.name!r} references unknown table {table!r}"
                        )
                    elif column not in physical[table]:
                        errors.append(
                            f"metric {metric.name!r} references unknown column "
                            f"{table}.{column}"
                        )
                    if table != entity.table:
                        errors.append(
                            f"metric {metric.name!r} MVP expression/filter must use its "
                            f"base entity table {entity.table!r}, not {table!r}"
                        )
                    if column in entity.hidden_columns:
                        errors.append(
                            f"metric {metric.name!r} references hidden column "
                            f"{table}.{column}"
                        )
            for dimension_ref in metric.allowed_dimensions:
                parsed_dimension = cls._parse_dimension_reference(dimension_ref)
                if parsed_dimension is None:
                    errors.append(
                        f"metric {metric.name!r} has invalid allowed dimension "
                        f"{dimension_ref!r}; expected entity.dimension"
                    )
                    continue
                entity_name, dimension_name = parsed_dimension
                dimension_entity = entities_by_name.get(entity_name)
                if dimension_entity is None:
                    errors.append(
                        f"metric {metric.name!r} allows dimension on unknown entity "
                        f"{entity_name!r}"
                    )
                    continue
                if not any(
                    dimension.name == dimension_name
                    for dimension in dimension_entity.dimensions
                ):
                    errors.append(
                        f"metric {metric.name!r} allows unknown dimension "
                        f"{dimension_ref!r}"
                    )
                if dimension_entity.table != entity.table:
                    resolved_path = cls.resolve_join_path(
                        model, metric.entity, entity_name
                    )
                    if resolved_path is None:
                        errors.append(
                            f"metric {metric.name!r} dimension {dimension_ref!r} has "
                            "no declared direct relationship or explicit join path"
                        )
                    elif not resolved_path.safe:
                        errors.append(
                            f"metric {metric.name!r} dimension {dimension_ref!r} "
                            "uses a fan-out join path: "
                            + ", ".join(resolved_path.fanout_steps)
                        )
            if metric.time_field:
                parsed_time = cls._parse_column_reference(metric.time_field)
                if parsed_time is None:
                    errors.append(
                        f"metric {metric.name!r} has invalid time_field "
                        f"{metric.time_field!r}; expected table.column"
                    )
                else:
                    table, column = parsed_time
                    if table != entity.table:
                        errors.append(
                            f"metric {metric.name!r} time_field must use base entity "
                            f"table {entity.table!r}"
                        )
                    if table not in physical or column not in physical.get(table, set()):
                        errors.append(
                            f"metric {metric.name!r} time_field references unknown "
                            f"column {table}.{column}"
                        )
        if errors:
            raise SemanticModelError(
                "Semantic model contradicts the physical SQLite schema: "
                + "; ".join(errors)
            )

    @classmethod
    def validate_policy_visibility(
        cls, model: SemanticModel, visible_schemas: list[Any]
    ) -> None:
        """Ensure governed semantics cannot reintroduce policy-hidden identifiers."""
        visible = {
            schema.table_name: {column.name for column in schema.columns}
            for schema in visible_schemas
        }
        errors: set[str] = set()
        entities = {entity.name: entity for entity in model.entities}
        for entity in model.entities:
            columns = visible.get(entity.table, set())
            required = {
                *entity.primary_key,
                *entity.grain,
                *(dimension.column for dimension in entity.dimensions),
            } - set(entity.hidden_columns)
            errors.update(
                f"{entity.table}.{column}" for column in required if column not in columns
            )
        for relationship in model.relationships:
            for reference in (relationship.from_ref, relationship.to_ref):
                parsed = cls._parse_column_reference(reference)
                if parsed and parsed[1] not in visible.get(parsed[0], set()):
                    errors.add(f"{parsed[0]}.{parsed[1]}")
        for metric in model.metrics:
            entity = entities.get(metric.entity)
            hidden = set(entity.hidden_columns) if entity else set()
            expressions = [metric.expression, *metric.default_filters]
            if metric.time_field:
                expressions.append(metric.time_field)
            for expression in expressions:
                for table, column in cls._extract_physical_references(expression):
                    if column not in visible.get(table, set()) and column not in hidden:
                        errors.add(f"{table}.{column}")
        if errors:
            raise SemanticModelError(
                "SQL security policy hides identifiers required by the semantic model: "
                + ", ".join(sorted(errors))
            )

    @classmethod
    def resolve_join_path(
        cls,
        model: SemanticModel,
        from_entity: str,
        to_entity: str,
        *,
        include_undeclared: bool = False,
    ) -> ResolvedJoinPath | None:
        """Resolve a governed path, optionally searching the graph for diagnostics."""
        entities = {entity.name: entity for entity in model.entities}
        if from_entity not in entities or to_entity not in entities:
            return None
        if from_entity == to_entity:
            table = entities[from_entity].table
            return ResolvedJoinPath(
                name=f"{from_entity}_self",
                from_entity=from_entity,
                to_entity=to_entity,
                tables=[table],
            )

        for join_path in model.join_paths:
            if (
                join_path.from_entity == from_entity
                and join_path.to_entity == to_entity
            ):
                return cls._resolve_relationship_sequence(
                    model,
                    from_entity,
                    to_entity,
                    join_path.relationships,
                    name=join_path.name,
                    explicit=True,
                )
            if (
                join_path.from_entity == to_entity
                and join_path.to_entity == from_entity
            ):
                return cls._resolve_relationship_sequence(
                    model,
                    from_entity,
                    to_entity,
                    list(reversed(join_path.relationships)),
                    name=f"{join_path.name}:reverse",
                    explicit=True,
                )

        direct_candidates: list[ResolvedJoinPath] = []
        for relationship in model.relationships:
            resolved = cls._resolve_relationship_sequence(
                model,
                from_entity,
                to_entity,
                [relationship.name],
                name=relationship.name,
                explicit=False,
            )
            if resolved is not None:
                direct_candidates.append(resolved)
        if direct_candidates:
            return sorted(direct_candidates, key=lambda item: not item.safe)[0]
        if not include_undeclared:
            return None

        relationships = model.relationships
        queue: list[tuple[str, list[str], set[str]]] = [
            (from_entity, [], {from_entity})
        ]
        while queue:
            current, path, visited = queue.pop(0)
            if len(path) >= 4:
                continue
            current_table = entities[current].table
            for relationship in relationships:
                from_ref = cls._parse_column_reference(relationship.from_ref)
                to_ref = cls._parse_column_reference(relationship.to_ref)
                if not from_ref or not to_ref:
                    continue
                if current_table == from_ref[0]:
                    next_table = to_ref[0]
                elif current_table == to_ref[0]:
                    next_table = from_ref[0]
                else:
                    continue
                next_entity = next(
                    (
                        entity.name
                        for entity in model.entities
                        if entity.table == next_table
                    ),
                    None,
                )
                if next_entity is None or next_entity in visited:
                    continue
                next_path = [*path, relationship.name]
                if next_entity == to_entity:
                    return cls._resolve_relationship_sequence(
                        model,
                        from_entity,
                        to_entity,
                        next_path,
                        name=f"diagnostic:{from_entity}_to_{to_entity}",
                        explicit=False,
                    )
                queue.append((next_entity, next_path, {*visited, next_entity}))
        return None

    @classmethod
    def _resolve_relationship_sequence(
        cls,
        model: SemanticModel,
        from_entity: str,
        to_entity: str,
        relationship_names: list[str],
        *,
        name: str,
        explicit: bool,
    ) -> ResolvedJoinPath | None:
        entities_by_name = {entity.name: entity for entity in model.entities}
        entities_by_table = {entity.table: entity for entity in model.entities}
        relationships = {
            relationship.name: relationship for relationship in model.relationships
        }
        current = entities_by_name.get(from_entity)
        target = entities_by_name.get(to_entity)
        if current is None or target is None:
            return None
        tables = [current.table]
        steps: list[ResolvedJoinStep] = []
        for relationship_name in relationship_names:
            relationship = relationships.get(relationship_name)
            if relationship is None:
                return None
            declared_from = cls._parse_column_reference(relationship.from_ref)
            declared_to = cls._parse_column_reference(relationship.to_ref)
            if not declared_from or not declared_to:
                return None
            if current.table == declared_from[0]:
                next_ref = declared_to
                current_ref = declared_from
                traversal: Literal["declared", "reverse"] = "declared"
                cardinality = relationship.relationship_type
            elif current.table == declared_to[0]:
                next_ref = declared_from
                current_ref = declared_to
                traversal = "reverse"
                cardinality = cls._reverse_cardinality(relationship.relationship_type)
            else:
                return None
            next_entity = entities_by_table.get(next_ref[0])
            if next_entity is None:
                return None
            fanout = cardinality in {"one_to_many", "many_to_many"}
            evidence = (
                f"{current.name} grain {current.effective_grain!r} -> "
                f"{next_entity.name} grain {next_entity.effective_grain!r}; "
                f"{cardinality} traversal"
            )
            steps.append(
                ResolvedJoinStep(
                    relationship=relationship.name,
                    from_entity=current.name,
                    from_table=current_ref[0],
                    from_column=current_ref[1],
                    to_entity=next_entity.name,
                    to_table=next_ref[0],
                    to_column=next_ref[1],
                    traversal=traversal,
                    cardinality=cardinality,
                    fanout=fanout,
                    source_grain=current.effective_grain,
                    target_grain=next_entity.effective_grain,
                    evidence=evidence,
                )
            )
            current = next_entity
            tables.append(current.table)
        if current.table != target.table:
            return None
        fanout_steps = [
            f"{step.relationship}: {step.from_entity} -> {step.to_entity} "
            f"is {step.cardinality} ({step.evidence})"
            for step in steps
            if step.fanout
        ]
        return ResolvedJoinPath(
            name=name,
            from_entity=from_entity,
            to_entity=to_entity,
            relationships=relationship_names,
            tables=tables,
            steps=steps,
            safe=not fanout_steps,
            fanout_steps=fanout_steps,
            explicit=explicit,
        )

    @staticmethod
    def _reverse_cardinality(relationship_type: str) -> str:
        return {
            "one_to_one": "one_to_one",
            "one_to_many": "many_to_one",
            "many_to_one": "one_to_many",
            "many_to_many": "many_to_many",
        }[relationship_type]

    @classmethod
    def match_question(
        cls, model: SemanticModel, question: str
    ) -> list[SemanticMatch]:
        normalized_question = SemanticModelLoader._normalize(question)
        matches: list[SemanticMatch] = []
        seen: set[tuple[str, str, str, str | None]] = set()
        for entity in model.entities:
            entity_terms = [
                term
                for term in [entity.name, *entity.synonyms]
                if SemanticModelLoader._contains_term(normalized_question, term)
            ]
            if entity_terms:
                term = max(entity_terms, key=lambda item: len(cls._normalize(item)))
                key = ("entity", entity.name, entity.table, None)
                matches.append(
                    SemanticMatch(
                        term=term,
                        kind="entity",
                        semantic_name=entity.name,
                        table=entity.table,
                    )
                )
                seen.add(key)
            for dimension in entity.dimensions:
                dimension_terms = [
                    term
                    for term in [dimension.name, *dimension.synonyms]
                    if SemanticModelLoader._contains_term(normalized_question, term)
                ]
                if dimension_terms:
                    term = max(
                        dimension_terms, key=lambda item: len(cls._normalize(item))
                    )
                    key = (
                        "dimension",
                        dimension.name,
                        entity.table,
                        dimension.column,
                    )
                    if key not in seen:
                        matches.append(
                            SemanticMatch(
                                term=term,
                                kind="dimension",
                                semantic_name=dimension.name,
                                table=entity.table,
                                column=dimension.column,
                            )
                        )
                        seen.add(key)
        return matches

    @staticmethod
    def match_metrics(model: SemanticModel, question: str) -> list[MetricMatch]:
        normalized_question = SemanticModelLoader._normalize(question)
        candidates: list[tuple[int, int, MetricMatch]] = []
        for metric_index, metric in enumerate(model.metrics):
            matching_terms = [
                term
                for term in [metric.name, *metric.synonyms]
                if SemanticModelLoader._contains_term(normalized_question, term)
            ]
            if matching_terms:
                term = max(matching_terms, key=lambda item: len(item))
                candidates.append(
                    (
                        -len(SemanticModelLoader._normalize(term)),
                        metric_index,
                        MetricMatch(matched_term=term, metric=metric),
                    )
                )
        candidates.sort(key=lambda item: (item[0], item[1]))
        return [item[2] for item in candidates[:3]]

    @staticmethod
    def _validate_columns(
        errors: list[str],
        table: str,
        label: str,
        requested: list[str],
        physical: set[str],
    ) -> None:
        for column in requested:
            if column not in physical:
                errors.append(
                    f"{label} references unknown column {table}.{column}"
                )

    @staticmethod
    def _parse_column_reference(reference: str) -> tuple[str, str] | None:
        if "." not in reference:
            return None
        table, column = reference.split(".", 1)
        if not table.strip() or not column.strip():
            return None
        table = table.strip().strip('"')
        column = column.strip().strip('"')
        return table, column

    @staticmethod
    def _parse_dimension_reference(reference: str) -> tuple[str, str] | None:
        return SemanticModelLoader._parse_column_reference(reference)

    @staticmethod
    def _extract_physical_references(expression: str) -> set[tuple[str, str]]:
        pattern = re.compile(
            r'\b([A-Za-z_][A-Za-z0-9_]*)\.(?:"([^"]+)"|([A-Za-z_][A-Za-z0-9_]*))'
        )
        return {
            (match.group(1), match.group(2) or match.group(3))
            for match in pattern.finditer(expression)
        }

    @staticmethod
    def _tables_related(
        left: str,
        right: str,
        relationships: list[SemanticRelationship],
    ) -> bool:
        for relationship in relationships:
            from_ref = SemanticModelLoader._parse_column_reference(
                relationship.from_ref
            )
            to_ref = SemanticModelLoader._parse_column_reference(relationship.to_ref)
            if from_ref and to_ref and {from_ref[0], to_ref[0]} == {left, right}:
                return True
        return False

    @staticmethod
    def _normalize(value: str) -> str:
        return " ".join(re.findall(r"[\w]+", value.casefold(), flags=re.UNICODE))

    @staticmethod
    def _contains_term(normalized_question: str, term: str) -> bool:
        normalized_term = SemanticModelLoader._normalize(term)
        if not normalized_term:
            return False
        if normalized_term.isascii():
            return f" {normalized_term} " in f" {normalized_question} "
        return normalized_term in normalized_question
