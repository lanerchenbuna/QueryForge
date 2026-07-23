"""Bounded source adapters for CSV, Parquet, and JSON API ingestion."""

from __future__ import annotations

import csv
import json
import os
import re
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

from queryforge.data_assets.models import AssetSource, DataAssetError


def read_source(source: AssetSource) -> list[dict[str, Any]]:
    if source.source_type == "csv":
        return _read_csv(expand_environment(source.path or ""))
    if source.source_type == "parquet":
        return _read_parquet(expand_environment(source.path or ""))
    return _read_api(source)


def _read_csv(path_value: str) -> list[dict[str, Any]]:
    path = Path(path_value).expanduser()
    if not path.is_file():
        raise DataAssetError(f"CSV source does not exist: {path}")
    with path.open("r", encoding="utf-8-sig", newline="") as source:
        return [dict(row) for row in csv.DictReader(source)]


def _read_parquet(path_value: str) -> list[dict[str, Any]]:
    path = Path(path_value).expanduser()
    if not path.is_file():
        raise DataAssetError(f"Parquet source does not exist: {path}")
    try:
        import pyarrow.parquet as parquet
    except ImportError as exc:
        raise DataAssetError(
            "Parquet ingestion requires pyarrow; install with "
            "pip install -r requirements-assets.txt"
        ) from exc
    return [dict(row) for row in parquet.read_table(path).to_pylist()]


def _read_api(source: AssetSource) -> list[dict[str, Any]]:
    url = expand_environment(source.url or "")
    headers = {
        key: expand_environment(value) for key, value in source.headers.items()
    }
    rows: list[dict[str, Any]] = []
    for page in range(1, source.max_pages + 1):
        parameters = {
            key: expand_environment(str(value))
            for key, value in source.params.items()
        }
        if source.page_param:
            parameters[source.page_param] = str(page)
        request_url = _with_query(url, parameters)
        request = urllib.request.Request(
            request_url,
            headers={"Accept": "application/json", **headers},
            method="GET",
        )
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except Exception as exc:
            raise DataAssetError(f"API ingestion failed for {url}: {exc}") from exc
        page_rows = _extract_records(payload, source.records_path)
        rows.extend(page_rows)
        if not source.page_param or len(page_rows) < source.page_size:
            break
    return rows


def _with_query(url: str, parameters: dict[str, str]) -> str:
    if not parameters:
        return url
    separator = "&" if "?" in url else "?"
    return f"{url}{separator}{urllib.parse.urlencode(parameters)}"


def _extract_records(
    payload: Any, records_path: str | None
) -> list[dict[str, Any]]:
    value = payload
    for part in (records_path or "").split("."):
        if not part:
            continue
        if not isinstance(value, dict) or part not in value:
            raise DataAssetError(
                f"API records_path does not exist: {records_path!r}"
            )
        value = value[part]
    if not isinstance(value, list) or not all(
        isinstance(row, dict) for row in value
    ):
        raise DataAssetError("API response records must be a JSON array of objects")
    return [dict(row) for row in value]


def expand_environment(value: str) -> str:
    def replace(match: re.Match[str]) -> str:
        name = match.group(1)
        if name not in os.environ:
            raise DataAssetError(
                f"Missing environment variable referenced by source: {name}"
            )
        return os.environ[name]

    return re.sub(r"\$\{([A-Z][A-Z0-9_]*)\}", replace, value)
