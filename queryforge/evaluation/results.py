"""Declared output contracts: column positions stay significant inside a row."""
from __future__ import annotations

import math
from typing import Any, Callable, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class ResultContract(BaseModel):
    model_config = ConfigDict(extra="forbid")
    required_columns: list[str] | None = None
    allow_extra_columns: bool = False
    row_order: Literal["ordered", "multiset"] = "multiset"
    empty: Literal["allow", "forbid"] = "allow"
    columns_path: str = "columns"
    rows_path: str = "rows"

    @field_validator("required_columns")
    @classmethod
    def unique_columns(cls, value):
        if value is not None and (not value or len(set(value)) != len(value) or any(not name.strip() for name in value)):
            raise ValueError("required_columns must be nonempty, unique column names")
        return value


def cells_match(actual: Any, expected: Any, tolerance: float = 1e-9) -> bool:
    if actual is None or expected is None or isinstance(actual, bool) or isinstance(expected, bool):
        return actual is expected
    if isinstance(actual, (int, float)) and isinstance(expected, (int, float)):
        if isinstance(actual, int) and isinstance(expected, int):
            return actual == expected
        try:
            return math.isfinite(actual) and math.isfinite(expected) and math.isclose(actual, expected, rel_tol=tolerance, abs_tol=tolerance)
        except OverflowError:
            return False
    return type(actual) is type(expected) and actual == expected


def compare_results(actual_rows, actual_columns, expected_rows, expected_columns, *,
                    contract: ResultContract | None = None,
                    cell_match: Callable[[Any, Any], bool] = cells_match,
                    positional_aliases: bool = False) -> bool:
    contract = contract or ResultContract()
    if (not isinstance(actual_rows, (list, tuple)) or not isinstance(expected_rows, (list, tuple))
            or (actual_columns is not None and not isinstance(actual_columns, (list, tuple)))
            or not isinstance(expected_columns, (list, tuple))):
        return False
    actual_columns, expected_columns = list(actual_columns or []), list(expected_columns or [])
    if not actual_columns and not actual_rows:
        # No values exist to establish even legacy positional projection shape.
        return False
    required = contract.required_columns or expected_columns
    if any(not isinstance(column, str) for column in [*actual_columns, *expected_columns]):
        return False
    if (len(set(actual_columns)) != len(actual_columns) or len(set(expected_columns)) != len(expected_columns)
            or not set(required).issubset(expected_columns)):
        return False
    expected_width = len(expected_columns)
    actual_width = len(actual_columns) if actual_columns else expected_width
    if any(not isinstance(row, (list, tuple)) or len(row) != actual_width for row in actual_rows):
        return False
    if any(not isinstance(row, (list, tuple)) or len(row) != expected_width for row in expected_rows):
        return False
    if actual_columns and set(required).issubset(actual_columns):
        if not contract.allow_extra_columns and set(actual_columns) != set(required):
            return False
        actual_positions = [actual_columns.index(column) for column in required]
        expected_positions = [expected_columns.index(column) for column in required]
    elif positional_aliases and contract.required_columns is None and actual_width == expected_width and (
        not actual_columns or not (set(actual_columns) & set(expected_columns))
    ):
        actual_positions = expected_positions = list(range(expected_width))
    else:
        return False
    if contract.empty == "forbid" and (not actual_rows or not expected_rows):
        return False
    if len(actual_rows) != len(expected_rows):
        return False
    def row_matches(actual, expected):
        return all(cell_match(actual[a], expected[e]) for a, e in zip(actual_positions, expected_positions))
    if contract.row_order == "ordered":
        return all(row_matches(a, e) for a, e in zip(actual_rows, expected_rows))
    remaining = list(actual_rows)
    for expected in expected_rows:
        match = next((i for i, row in enumerate(remaining) if row_matches(row, expected)), None)
        if match is None:
            return False
        remaining.pop(match)
    return True
