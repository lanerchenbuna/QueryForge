"""Declarative contracts for the local data-asset build pipeline."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from queryforge.domain.semantic import (
    ContractQualityRule,
    SemanticJoinPath,
    SemanticMetric,
    SemanticRelationship,
)


class DataAssetError(ValueError):
    """Raised when an asset source, contract, or publication is invalid."""


class StrictAssetModel(BaseModel):
    """Reject misspelled YAML keys before data is written."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)


class AssetSource(StrictAssetModel):
    """One bounded batch source: local CSV/Parquet or a JSON HTTP endpoint."""

    source_type: Literal["csv", "parquet", "api"] = Field(alias="type")
    path: str | None = None
    url: str | None = None
    records_path: str | None = None
    headers: dict[str, str] = Field(default_factory=dict)
    params: dict[str, str | int | float | bool] = Field(default_factory=dict)
    page_param: str | None = None
    page_size: int = Field(default=100, ge=1, le=100_000)
    max_pages: int = Field(default=1, ge=1, le=10_000)

    @model_validator(mode="after")
    def validate_location(self) -> "AssetSource":
        if self.source_type in {"csv", "parquet"} and not self.path:
            raise ValueError(f"{self.source_type} source requires path")
        if self.source_type == "api" and not self.url:
            raise ValueError("api source requires url")
        if self.source_type != "api" and (
            self.headers or self.params or self.page_param
        ):
            raise ValueError("headers, params, and pagination are only valid for api")
        return self


class AssetQualityRules(StrictAssetModel):
    """Deterministic checks performed before a row is published."""

    required_columns: list[str] = Field(default_factory=list)
    unique_key: list[str] = Field(default_factory=list)
    max_invalid_ratio: float = Field(default=1.0, ge=0.0, le=1.0)


class AssetSemanticSpec(StrictAssetModel):
    """Required business meaning for one uploaded asset."""

    entity_name: str = Field(min_length=1)
    entity_type: Literal["dimension", "fact"] = "dimension"
    description: str = Field(min_length=1)
    primary_key: list[str] = Field(default_factory=list)
    grain: list[str] = Field(default_factory=list)
    hidden_columns: list[str] = Field(default_factory=list)
    dimensions: list[str] = Field(min_length=1)
    contract_version: str = "1.0"
    owner: str = Field(min_length=1)
    sla: str | None = None
    refresh_frequency: str | None = None
    sensitivity: Literal["public", "internal", "confidential", "restricted"] = (
        "internal"
    )
    quality_rules: list[ContractQualityRule] = Field(default_factory=list)

    @field_validator("entity_name", "description", "owner")
    @classmethod
    def trim_required_semantic_text(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("must not be blank")
        return normalized

    @model_validator(mode="after")
    def validate_fact_grain(self) -> "AssetSemanticSpec":
        if self.entity_type == "fact" and not (self.primary_key or self.grain):
            raise ValueError("fact semantic entities require primary_key or grain")
        return self


class AssetSemanticModelSpec(StrictAssetModel):
    """Reviewed model-level semantics published with an upload batch."""

    name: str = Field(min_length=1)
    description: str = Field(min_length=1)
    owner: str = Field(min_length=1)
    reviewed: bool = False
    auto_count_metrics: bool = True
    relationships: list[SemanticRelationship] = Field(default_factory=list)
    join_paths: list[SemanticJoinPath] = Field(default_factory=list)
    metrics: list[SemanticMetric] = Field(default_factory=list)

    @field_validator("name", "description", "owner")
    @classmethod
    def trim_required_model_text(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("must not be blank")
        return normalized


class DataAssetSpec(StrictAssetModel):
    """A source-to-table data contract."""

    name: str = Field(min_length=1)
    source: AssetSource
    target_table: str | None = None
    column_aliases: dict[str, str] = Field(default_factory=dict)
    watermark_field: str | None = None
    quality: AssetQualityRules = Field(default_factory=AssetQualityRules)
    semantic: AssetSemanticSpec
    publish_mode: Literal["append", "replace"] = "append"

    @field_validator("name", "target_table", "watermark_field")
    @classmethod
    def trim_optional_names(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        if not normalized:
            raise ValueError("must not be blank")
        return normalized

    @model_validator(mode="after")
    def prevent_incremental_replace_loss(self) -> "DataAssetSpec":
        if self.watermark_field and self.publish_mode == "replace":
            raise ValueError(
                "watermark_field cannot be combined with publish_mode=replace"
            )
        return self


class AssetBuildConfig(StrictAssetModel):
    """The YAML root consumed by :class:`DataAssetBuilder`."""

    version: int = Field(default=1, ge=1)
    semantic_model: AssetSemanticModelSpec
    assets: list[DataAssetSpec] = Field(min_length=1)
    semantic_model_output: str | None = None

    @model_validator(mode="after")
    def validate_unique_assets(self) -> "AssetBuildConfig":
        names = [asset.name for asset in self.assets]
        if len(names) != len(set(names)):
            raise ValueError("asset names must be unique")
        targets = [asset.target_table or asset.name for asset in self.assets]
        if len(targets) != len(set(targets)):
            raise ValueError("target tables must be unique")
        return self


class AssetBuildResult(BaseModel):
    """Auditable result emitted for one data-asset build."""

    asset_name: str
    run_id: str
    source_type: str
    source_location: str
    target_table: str
    input_rows: int = 0
    staged_rows: int = 0
    published_rows: int = 0
    quarantined_rows: int = 0
    watermark_before: str | None = None
    watermark_after: str | None = None
    publish_database: str
    staging_database: str
    metadata_database: str
    semantic_model_path: str | None = None
    status: Literal["success", "failed"] = "success"
    error: str | None = None
    details: dict[str, Any] = Field(default_factory=dict)
