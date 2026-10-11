"""Strict semantic-layer and operational-contract data structures."""

from __future__ import annotations

from typing import Literal
from datetime import date
import re

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


class RatioComponents(StrictSemanticModel):
    """Aggregate both populations at the requested grain before division."""

    numerator: str = Field(min_length=1)
    denominator: str = Field(min_length=1)
    scale: float = Field(default=1.0, gt=0, allow_inf_nan=False)

    @field_validator("numerator", "denominator")
    @classmethod
    def aggregate_expression(cls, value: str) -> str:
        import sqlglot
        from sqlglot import exp

        nodes = [node for node in sqlglot.parse(value, read="sqlite") if node is not None]
        if len(nodes) != 1 or any(marker in value for marker in (";", "--", "/*", "*/")):
            raise ValueError("ratio components must be one expression without comments")
        node = nodes[0]
        # A component is one aggregate over rows, never an average of ratios or
        # an unrelated subquery population. More complex definitions need review.
        if not isinstance(node, (exp.Sum, exp.Count)) or any(
            isinstance(child, (exp.Select, exp.Div, exp.AggFunc))
            for child in node.walk() if child is not node
        ):
            raise ValueError("ratio components must be a single SUM or COUNT aggregate")
        if any(not column.table or column.db or column.catalog for column in node.find_all(exp.Column)):
            raise ValueError("ratio component columns must be qualified table.column references")
        return node.sql(dialect="sqlite")

    @property
    def expression(self) -> str:
        return (f"CAST({self.numerator} AS REAL) * {self.scale} / "
                f"NULLIF({self.denominator}, 0)")


class MetricTimeContract(StrictSemanticModel):
    role: str = Field(min_length=1)
    timezone: str = "UTC"
    fiscal_year_start_month: int = Field(default=1, ge=1, le=12)
    coverage_start: str | None = None
    complete_through: str | None = None
    require_complete_periods: bool = False

    @field_validator("role")
    @classmethod
    def nonblank_role(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("time role must not be blank")
        return value.strip()

    @field_validator("timezone")
    @classmethod
    def supported_timezone(cls, value: str) -> str:
        if value == "UTC":
            return value
        if not re.fullmatch(r"[+-](?:0\d|1[0-3]):[0-5]\d|[+-]14:00", value):
            raise ValueError("SQLite time contracts support UTC or fixed offsets (+08:00); named DST zones are unsupported")
        return value

    @model_validator(mode="after")
    def coverage_contract(self) -> "MetricTimeContract":
        if bool(self.coverage_start) != bool(self.complete_through):
            raise ValueError("coverage_start and complete_through must be declared together")
        if self.require_complete_periods and not self.coverage_start:
            raise ValueError("complete period enforcement requires declared coverage bounds")
        if self.coverage_start:
            start, end = date.fromisoformat(self.coverage_start), date.fromisoformat(self.complete_through)
            if start > end:
                raise ValueError("coverage_start must not exceed complete_through")
        return self


class MetricAnalysisContract(StrictSemanticModel):
    unit: str = Field(min_length=1)
    additivity: Literal["additive", "non_additive", "semi_additive"]
    non_additive_dimensions: list[str] = Field(default_factory=list)
    time: MetricTimeContract | None = None

    @field_validator("unit")
    @classmethod
    def nonblank_unit(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("metric unit must not be blank")
        return value.strip()

    @model_validator(mode="after")
    def semi_additive_dimensions(self) -> "MetricAnalysisContract":
        if self.additivity == "semi_additive" and not self.non_additive_dimensions:
            raise ValueError("semi_additive metrics require non_additive_dimensions")
        return self


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
    ratio: RatioComponents | None = None
    analysis: MetricAnalysisContract | None = None

    @model_validator(mode="before")
    @classmethod
    def materialize_ratio(cls, data):
        if isinstance(data, dict) and data.get("ratio") is not None:
            data = dict(data)
            ratio = RatioComponents.model_validate(data["ratio"])
            canonical = ratio.expression
            if data.get("expression") and data["expression"] != canonical:
                raise ValueError("ratio expression must match its declared components; omit expression to derive it")
            data["expression"] = canonical
        return data

    @model_validator(mode="after")
    def analysis_consistency(self) -> "SemanticMetric":
        if self.ratio and self.aggregation != "ratio":
            raise ValueError("ratio components require ratio aggregation")
        if self.analysis:
            distinct = bool(re.search(r"\bDISTINCT\b", self.expression, re.I))
            if (self.aggregation == "ratio" or distinct) and self.analysis.additivity != "non_additive":
                raise ValueError("ratios and distinct counts must be non_additive")
            if self.analysis.time and not self.time_field:
                raise ValueError("a time contract requires time_field")
            if (self.analysis.time and self.time_field.endswith("_key")
                    and self.analysis.time.timezone != "UTC"):
                raise ValueError("date keys already represent calendar dates; timezone shifts require timestamps")
        return self

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
