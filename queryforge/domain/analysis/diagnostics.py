"""Reconcile category changes with independently queried period totals."""
from __future__ import annotations

import json
import math
from typing import Any

from .analysis_tools import numbers_close


def reconcile_buckets(dataset: dict[str, Any], totals: dict[str, Any], metric: str,
                      current_period: str, baseline_period: str) -> dict[str, Any]:
    """Prove both period populations before decomposing their difference.

    Empty category buckets are zero only with enforced ingestion coverage and
    complete, unbounded query results. Independently queried totals also catch
    rows lost at dimension joins, even when those losses cancel in the delta.
    """
    if any(source.get("truncated") or (
        (source.get("query_spec") or {}).get("limit")
        and len(source.get("rows", [])) >= source["query_spec"]["limit"]
    ) for source in (dataset, totals)):
        raise ValueError("unverified_reconciliation: bounded results cannot prove all categories")
    metadata = dataset.get("metric_contracts", {}).get(metric, {})
    if metadata.get("additivity") != "additive" or any(
        ref in metadata.get("non_additive_dimensions", []) for ref in dataset.get("dimensions", [])
    ):
        raise ValueError("unsupported_additivity: category deltas require an additive metric")
    if (dataset.get("filters") != totals.get("filters")
            or metadata != totals.get("metric_contracts", {}).get(metric)
            or dataset.get("time_grain") != totals.get("time_grain")
            or dataset.get("time_filter") != totals.get("time_filter")):
        raise ValueError("unverified_reconciliation: totals and buckets have different populations")
    dimensions = dataset.get("dimensions", [])
    if len(dimensions) < 2 or totals.get("dimensions") != dimensions[:1]:
        raise ValueError("unsupported_grain: need one time dimension followed by category dimensions")
    def measure(row, columns):
        value = row[columns.index(metric)]
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
            raise ValueError("unverified_measure: category values must be finite numbers")
        return value
    total_values = {}
    for row in totals.get("rows", []):
        period = str(row[0])
        if period in total_values:
            raise ValueError("unverified_reconciliation: duplicate total period")
        total_values[period] = measure(row, totals["columns"])
    if current_period not in total_values or baseline_period not in total_values:
        raise ValueError("data_absent: independent current or baseline total is missing")
    by_category = {}
    for row in dataset.get("rows", []):
        period = str(row[0])
        if period not in {current_period, baseline_period}:
            continue
        key_values = list(row[1:len(dimensions)])
        key = json.dumps(key_values, ensure_ascii=False, separators=(",", ":"))
        group = by_category.setdefault(key, {"dimensions": dict(zip(dimensions[1:], key_values)), "periods": {}})
        if period in group["periods"]:
            raise ValueError("unverified_reconciliation: duplicate category-period key")
        group["periods"][period] = measure(row, dataset["columns"])
    if not by_category:
        raise ValueError("data_absent: no category values for the requested periods")
    complete = dataset.get("period_completeness") == totals.get("period_completeness") == "complete"
    buckets, empty_buckets = [], []
    for key, group in sorted(by_category.items()):
        for period in (current_period, baseline_period):
            if period not in group["periods"]:
                if not complete:
                    raise ValueError("unverified_empty_bucket: missing category period without complete coverage")
                group["periods"][period] = 0
                empty_buckets.append({"category": key, "period": period, "reason": "empty_bucket_in_complete_population"})
        buckets.append({"category": key, "current": group["periods"][current_period],
                        "baseline": group["periods"][baseline_period], "dimensions": group["dimensions"]})
    current = math.fsum(bucket["current"] for bucket in buckets)
    baseline = math.fsum(bucket["baseline"] for bucket in buckets)
    if not (numbers_close(current, total_values[current_period]) and numbers_close(baseline, total_values[baseline_period])):
        raise ValueError("unverified_reconciliation: category sums disagree with independent period totals")
    return {"buckets": buckets, "current_period": current_period, "baseline_period": baseline_period,
            "independent_current": total_values[current_period], "independent_baseline": total_values[baseline_period],
            "bucket_current_total": current, "bucket_baseline_total": baseline,
            "residual_current": current - total_values[current_period],
            "residual_baseline": baseline - total_values[baseline_period],
            "expected_total_delta": total_values[current_period] - total_values[baseline_period],
            "empty_buckets": empty_buckets}
