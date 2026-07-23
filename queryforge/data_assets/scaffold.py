"""Generate a reviewable data-asset and semantic-layer contract from a local file."""

from __future__ import annotations

import csv
import os
from pathlib import Path
from typing import Any

import yaml

from queryforge.data_assets.transforms import normalize_identifier
from queryforge.domain.semantic.builder import PII_COLUMN_PATTERN


class AssetScaffoldError(ValueError):
    """Raised when a local source cannot produce a safe semantic draft."""


def scaffold_asset_config(
    source_path: str | Path,
    *,
    output_path: str | Path,
    asset_name: str | None = None,
    target_table: str | None = None,
    entity_type: str = "auto",
    grain: list[str] | None = None,
    owner: str = "data-platform",
    sample_rows: int = 5_000,
) -> dict[str, Any]:
    """Inspect a CSV or Parquet file and return a mandatory-semantic draft."""

    source = Path(source_path).expanduser().resolve()
    output = Path(output_path).expanduser().resolve()
    if not source.is_file():
        raise AssetScaffoldError(f"Source file does not exist: {source}")
    if source.suffix.lower() not in {".csv", ".parquet"}:
        raise AssetScaffoldError("Semantic scaffolding supports .csv and .parquet files")
    raw_columns, rows = _read_sample(source, sample_rows)
    if not raw_columns:
        raise AssetScaffoldError(f"Source has no columns: {source}")

    aliases = {
        column: normalize_identifier(column)
        for column in raw_columns
    }
    columns = list(aliases.values())
    if len(columns) != len(set(columns)):
        raise AssetScaffoldError(
            "Source columns collide after snake_case normalization; provide "
            "explicit column_aliases before upload."
        )
    normalized_rows = [
        {aliases[column]: row.get(column) for column in raw_columns}
        for row in rows
    ]
    normalized_name = normalize_identifier(asset_name or source.stem)
    resolved_type = _entity_type(
        entity_type,
        target_table or normalized_name,
        normalized_name,
        columns,
    )
    resolved_grain = [
        normalize_identifier(column) for column in (grain or [])
    ] or _infer_grain(columns, normalized_rows)
    if resolved_type == "fact" and not resolved_grain:
        raise AssetScaffoldError(
            "No reliable unique grain was found for this fact asset. Re-run with "
            "--grain COLUMN after choosing the business event identifier."
        )
    hidden = [column for column in columns if PII_COLUMN_PATTERN.search(column)]
    dimensions = [column for column in columns if column not in hidden]
    if not dimensions:
        raise AssetScaffoldError(
            "All columns look sensitive; declare an analytical dimension before upload."
        )
    entity_name = _entity_name(normalized_name)
    target = normalize_identifier(
        target_table
        or f"{'fact' if resolved_type == 'fact' else 'dim'}_{entity_name}"
    )
    source_ref = os.path.relpath(source, output.parent)
    return {
        "version": 1,
        "semantic_model": {
            "name": f"{entity_name}_semantic_layer",
            "description": (
                f"Semantic layer for {entity_name}; review business definitions "
                "before publication."
            ),
            "owner": owner,
            "reviewed": False,
            "auto_count_metrics": True,
            "relationships": [],
            "join_paths": [],
            "metrics": [],
        },
        "assets": [
            {
                "name": normalized_name,
                "target_table": target,
                "source": {
                    "type": source.suffix.lower().lstrip("."),
                    "path": source_ref,
                },
                "column_aliases": aliases,
                "quality": {
                    "required_columns": list(resolved_grain),
                    "unique_key": list(resolved_grain),
                    "max_invalid_ratio": 0.05,
                },
                "semantic": {
                    "entity_name": entity_name,
                    "entity_type": resolved_type,
                    "description": (
                        f"{entity_name.replace('_', ' ').title()} uploaded from "
                        f"{source.name}."
                    ),
                    "primary_key": (
                        list(resolved_grain)
                        if resolved_type == "dimension"
                        else []
                    ),
                    "grain": list(resolved_grain),
                    "hidden_columns": hidden,
                    "dimensions": dimensions,
                    "owner": owner,
                    "sensitivity": "confidential" if hidden else "internal",
                },
            }
        ],
    }


def write_asset_scaffold(
    source_path: str | Path,
    output_path: str | Path,
    *,
    force: bool = False,
    **kwargs: Any,
) -> Path:
    """Write a draft contract without silently replacing an existing file."""

    output = Path(output_path).expanduser().resolve()
    if output.exists() and not force:
        raise AssetScaffoldError(
            f"Output already exists: {output}; pass force=True to replace it"
        )
    payload = scaffold_asset_config(
        source_path,
        output_path=output,
        **kwargs,
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        yaml.safe_dump(payload, allow_unicode=True, sort_keys=False, width=100),
        encoding="utf-8",
    )
    return output


def _read_sample(
    source: Path, limit: int
) -> tuple[list[str], list[dict[str, Any]]]:
    if limit < 1 or limit > 100_000:
        raise AssetScaffoldError("sample_rows must be between 1 and 100000")
    if source.suffix.lower() == ".csv":
        with source.open("r", encoding="utf-8-sig", newline="") as handle:
            reader = csv.DictReader(handle)
            columns = list(reader.fieldnames or [])
            rows = []
            for index, row in enumerate(reader):
                if index >= limit:
                    break
                rows.append(dict(row))
            return columns, rows
    try:
        import pyarrow.parquet as parquet
    except ImportError as exc:
        raise AssetScaffoldError(
            "Parquet scaffolding requires pyarrow; install requirements-assets.txt"
        ) from exc
    table = parquet.read_table(source)
    columns = list(table.column_names)
    return columns, table.slice(0, limit).to_pylist()


def _infer_grain(
    columns: list[str], rows: list[dict[str, Any]]
) -> list[str]:
    if not rows:
        return []
    preferred = [
        column
        for column in columns
        if column == "id" or column.endswith("_id")
    ]
    for column in [*preferred, *columns]:
        values = [row.get(column) for row in rows]
        if all(value not in {None, ""} for value in values) and len(set(values)) == len(
            values
        ):
            return [column]
    return []


def _entity_type(
    requested: str,
    target: str,
    name: str,
    columns: list[str],
) -> str:
    if requested not in {"auto", "fact", "dimension"}:
        raise AssetScaffoldError("entity_type must be auto, fact, or dimension")
    if requested != "auto":
        return requested
    value = f"{target} {name}".lower()
    event_words = (
        "event",
        "session",
        "order",
        "rating",
        "transaction",
        "watch",
        "impression",
        "follow",
    )
    has_time = any(
        token in column
        for column in columns
        for token in ("date", "time", "timestamp", "_at")
    )
    return "fact" if value.startswith("fact_") or any(
        word in value for word in event_words
    ) or has_time else "dimension"


def _entity_name(value: str) -> str:
    for prefix in ("fact_", "dim_", "bridge_", "stg_", "raw_"):
        if value.startswith(prefix):
            return value[len(prefix) :]
    return value
