"""Deterministic field normalization, quality checks, and watermark helpers."""

from __future__ import annotations

import json
import re
import unicodedata
from decimal import Decimal, InvalidOperation
from typing import Any

from queryforge.data_assets.models import DataAssetSpec


def normalize_record(
    record: dict[str, Any], aliases: dict[str, str]
) -> dict[str, Any]:
    normalized: dict[str, Any] = {}
    for index, (raw_name, raw_value) in enumerate(record.items(), start=1):
        source_name = str(raw_name)
        alias = aliases.get(source_name) or aliases.get(normalize_identifier(source_name))
        column = normalize_identifier(alias or source_name, fallback=f"column_{index}")
        while column in normalized:
            column = f"{column}_{index}"
        normalized[column] = normalize_value(raw_value)
    return normalized


def normalize_value(value: Any) -> Any:
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    text = str(value).strip()
    if not text:
        return None
    if re.fullmatch(r"[+-]?\d+", text):
        try:
            return int(text)
        except ValueError:
            return text
    if re.fullmatch(r"[+-]?(?:\d+\.\d*|\d*\.\d+)", text):
        try:
            return float(text)
        except ValueError:
            return text
    return text


def apply_quality_rules(
    rows: list[dict[str, Any]],
    asset: DataAssetSpec,
    existing_keys: set[tuple[Any, ...]],
) -> tuple[list[dict[str, Any]], list[tuple[dict[str, Any], str]]]:
    required = normalize_fields(asset.quality.required_columns)
    unique_key = normalize_fields(asset.quality.unique_key)
    seen = set(existing_keys)
    accepted: list[dict[str, Any]] = []
    quarantined: list[tuple[dict[str, Any], str]] = []
    for row in rows:
        missing = [column for column in required if row.get(column) is None]
        if missing:
            quarantined.append(
                (row, f"missing required columns: {', '.join(missing)}")
            )
            continue
        if unique_key:
            if any(row.get(column) is None for column in unique_key):
                quarantined.append((row, "unique key contains null"))
                continue
            key = tuple(row[column] for column in unique_key)
            if key in seen:
                quarantined.append((row, "duplicate unique key"))
                continue
            seen.add(key)
        accepted.append(row)
    return accepted, quarantined


def is_after_watermark(
    row: dict[str, Any],
    watermark_field: str | None,
    watermark_before: str | None,
) -> bool:
    if not watermark_field or watermark_before is None:
        return True
    value = row.get(normalize_identifier(watermark_field))
    return value is not None and compare_values(value, watermark_before) > 0


def max_watermark(
    rows: list[dict[str, Any]],
    watermark_field: str | None,
    previous: str | None,
) -> str | None:
    if not watermark_field:
        return previous
    field = normalize_identifier(watermark_field)
    values = [str(row[field]) for row in rows if row.get(field) is not None]
    if not values:
        return previous
    candidate = max(values, key=_ComparableWatermark)
    return (
        candidate
        if previous is None or compare_values(candidate, previous) > 0
        else previous
    )


class _ComparableWatermark:
    def __init__(self, value: str) -> None:
        self.value = value

    def __lt__(self, other: "_ComparableWatermark") -> bool:
        return compare_values(self.value, other.value) < 0


def compare_values(left: Any, right: Any) -> int:
    try:
        decimal_left = Decimal(str(left))
        decimal_right = Decimal(str(right))
        return (decimal_left > decimal_right) - (decimal_left < decimal_right)
    except (InvalidOperation, ValueError):
        left_text = str(left)
        right_text = str(right)
        return (left_text > right_text) - (left_text < right_text)


def normalize_fields(fields: list[str]) -> list[str]:
    return [normalize_identifier(field) for field in fields]


def normalize_contract_rules(
    rules: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    normalized = []
    for rule in rules:
        item = dict(rule)
        if item.get("column"):
            item["column"] = normalize_identifier(str(item["column"]))
        if item.get("referenced_column"):
            item["referenced_column"] = normalize_identifier(
                str(item["referenced_column"])
            )
        normalized.append(item)
    return normalized


def normalize_identifier(value: str, fallback: str = "column") -> str:
    ascii_value = (
        unicodedata.normalize("NFKD", str(value))
        .encode("ascii", "ignore")
        .decode("ascii")
    )
    identifier = re.sub(r"[^a-zA-Z0-9]+", "_", ascii_value).strip("_").lower()
    identifier = identifier or fallback
    return f"field_{identifier}" if identifier[0].isdigit() else identifier
