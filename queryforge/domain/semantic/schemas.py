"""Strict semantic-layer and operational-contract data structures."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class StrictSemanticModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ContractQualityRule(StrictSemanticModel):
    rule: Literal["null_rate", "unique", "range", "foreign_key"]
    column: str | None = None
    max_null_rate: float | None = Field(default=None, ge=0.0, le=1.0)
    minimum: float | None = None
    maximum: float | None = None
    referenced_table: str | None = None
    referenced_column: str | None = None
    severity: Literal["error", "warning"] = "error"

    @model_validator(mode="after")
    def validate_rule_configuration(self) -> "ContractQualityRule":
        if self.rule == "null_rate" and self.max_null_rate is None:
            raise ValueError("null_rate quality rules require max_null_rate")
        if self.rule == "range" and self.minimum is None and self.maximum is None:
            raise ValueError("range quality rules require minimum or maximum")
        if (
            self.rule == "range"
            and self.minimum is not None
            and self.maximum is not None
            and self.minimum > self.maximum
        ):
            raise ValueError("range quality rule minimum must not exceed maximum")
        if self.rule == "foreign_key" and (
            not self.referenced_table or not self.referenced_column
        ):
            raise ValueError(
                "foreign_key quality rules require referenced_table and "
                "referenced_column"
            )
        return self


class OperationalContract(StrictSemanticModel):
    contract_version: str = "1.0"
    owner: str | None = None
    sla: str | None = None
    refresh_frequency: str | None = None
    sensitivity: Literal["public", "internal", "confidential", "restricted"] = (
        "internal"
    )
    quality_rules: list[ContractQualityRule] = Field(default_factory=list)

    @field_validator("contract_version", "owner", "sla", "refresh_frequency")
    @classmethod
    def normalize_contract_text(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip()
        if not value:
            raise ValueError("must not be blank")
        return value


class SemanticDimension(OperationalContract):
    name: str = Field(min_length=1)
    column: str = Field(min_length=1)
    description: str = ""
    synonyms: list[str] = Field(default_factory=list)

    @field_validator("name", "column")
    @classmethod
    def strip_required_text(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("must not be blank")
        return value


class SemanticEntity(OperationalContract):
    name: str = Field(min_length=1)
    table: str = Field(min_length=1)
    description: str = ""
    entity_type: Literal["dimension", "fact"] = "dimension"
    synonyms: list[str] = Field(default_factory=list)
    primary_key: list[str] = Field(default_factory=list)
    grain: list[str] = Field(default_factory=list)
    hidden_columns: list[str] = Field(default_factory=list)
    expected_columns: list[str] = Field(default_factory=list)
    allow_additive_columns: bool = True
    dimensions: list[SemanticDimension] = Field(default_factory=list)

    @field_validator("name", "table")
    @classmethod
    def strip_required_text(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("must not be blank")
        return value

    @property
    def effective_grain(self) -> list[str]:
        return self.grain or self.primary_key


class CardinalityContract(StrictSemanticModel):
    source: Literal["one", "many"]
    target: Literal["one", "many"]
    enforcement: Literal["semantic", "physical_fk"] = "semantic"

    @property
    def relationship_type(self) -> str:
        return f"{self.source}_to_{self.target}"


class SemanticRelationship(StrictSemanticModel):
    name: str = Field(min_length=1)
    from_ref: str = Field(alias="from", min_length=3)
    to_ref: str = Field(alias="to", min_length=3)
    relationship_type: Literal[
        "one_to_one", "one_to_many", "many_to_one", "many_to_many"
    ] = "many_to_one"
    cardinality_contract: CardinalityContract | None = None
    description: str = ""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    @model_validator(mode="after")
    def contract_matches_relationship_type(self) -> "SemanticRelationship":
        if (
            self.cardinality_contract is not None
            and self.cardinality_contract.relationship_type != self.relationship_type
        ):
            raise ValueError(
                "cardinality_contract source/target must match relationship_type"
            )
        return self

    @property
    def effective_contract(self) -> CardinalityContract:
        if self.cardinality_contract is not None:
            return self.cardinality_contract
        source, _, target = self.relationship_type.partition("_to_")
        return CardinalityContract(source=source, target=target)


class SemanticJoinPath(OperationalContract):
    name: str = Field(min_length=1)
    from_entity: str = Field(min_length=1)
    to_entity: str = Field(min_length=1)
    relationships: list[str] = Field(min_length=1)
    description: str = ""


class ResolvedJoinStep(StrictSemanticModel):
    relationship: str
    from_entity: str
    from_table: str
    from_column: str
    to_entity: str
    to_table: str
    to_column: str
    traversal: Literal["declared", "reverse"]
    cardinality: Literal[
        "one_to_one", "one_to_many", "many_to_one", "many_to_many"
    ]
    fanout: bool
    source_grain: list[str] = Field(default_factory=list)
    target_grain: list[str] = Field(default_factory=list)
    evidence: str


class ResolvedJoinPath(StrictSemanticModel):
    name: str
    from_entity: str
    to_entity: str
    relationships: list[str] = Field(default_factory=list)
    tables: list[str] = Field(default_factory=list)
    steps: list[ResolvedJoinStep] = Field(default_factory=list)
    safe: bool = True
    fanout_steps: list[str] = Field(default_factory=list)
    explicit: bool = False


class SemanticMetric(OperationalContract):
    name: str = Field(min_length=1)
    description: str = Field(min_length=1)
    entity: str = Field(min_length=1)
    aggregation: Literal["count", "sum", "ratio"]
    expression: str = Field(min_length=1)
    synonyms: list[str] = Field(default_factory=list)
    default_filters: list[str] = Field(default_factory=list)
    allowed_dimensions: list[str] = Field(default_factory=list)
    time_field: str | None = None

    @field_validator("name", "description", "entity", "expression")
    @classmethod
    def strip_required_text(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("must not be blank")
        return value


class SemanticModel(StrictSemanticModel):
    version: int = Field(default=1, ge=1)
    name: str = Field(min_length=1)
    description: str = ""
    entities: list[SemanticEntity] = Field(min_length=1)
    relationships: list[SemanticRelationship] = Field(default_factory=list)
    join_paths: list[SemanticJoinPath] = Field(default_factory=list)
    metrics: list[SemanticMetric] = Field(default_factory=list)


class SemanticMatch(StrictSemanticModel):
    term: str
    kind: Literal["entity", "dimension"]
    semantic_name: str
    table: str
    column: str | None = None


class MetricMatch(StrictSemanticModel):
    matched_term: str
    metric: SemanticMetric


class SemanticModelContext(StrictSemanticModel):
    source_path: str
    model: SemanticModel
    matches: list[SemanticMatch] = Field(default_factory=list)

    def hidden_column_refs(self) -> set[tuple[str, str]]:
        return {
            (entity.table, column)
            for entity in self.model.entities
            for column in entity.hidden_columns
        }
