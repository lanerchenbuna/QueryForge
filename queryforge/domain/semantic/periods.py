"""Calendar arithmetic shared by deterministic compilation and comparison.

Week labels are Monday dates. Fiscal quarters/years use their start dates.
Coverage is an authored ingestion guarantee, not inferred from sparse fact rows.
"""
from __future__ import annotations

import calendar
from datetime import date, timedelta

from .query_constraints import UnsupportedConstraint


def time_contract(metric):
    return metric.analysis.time if metric.analysis else None


def calendar_source_sql(reference):
    text = f"CAST({reference} AS TEXT)"
    return (f"CASE WHEN {text} GLOB '[0-9][0-9][0-9][0-9][0-9][0-9][0-9][0-9]' "
              f"THEN SUBSTR({text}, 1, 4) || '-' || SUBSTR({text}, 5, 2) || '-' || SUBSTR({text}, 7, 2) "
            f"ELSE {text} END")


def calendar_sql(reference, contract=None):
    source = calendar_source_sql(reference)
    modifier = f", '{contract.timezone}'" if contract and contract.timezone != "UTC" else ""
    return f"DATE({source}{modifier})"


def invalid_time_sql(reference, contract=None):
    source = calendar_source_sql(reference)
    civil = f"SUBSTR({source}, 1, 10)"
    return f"({calendar_sql(reference, contract)} IS NULL OR DATE({civil}) IS NULL OR DATE({civil}) != {civil})"


def bucket_sql(reference, grain, contract=None):
    day = calendar_sql(reference, contract)
    fiscal = contract.fiscal_year_start_month if contract else 1
    if grain == "weekly":
        return f"DATE({day}, '-' || ((CAST(STRFTIME('%w', {day}) AS INTEGER) + 6) % 7) || ' days')"
    formats = {"daily": "%Y-%m-%d", "monthly": "%Y-%m", "yearly": "%Y"}
    if fiscal != 1 and grain in {"quarterly", "yearly"}:
        shifted = f"DATE({day}, 'start of month', '-{fiscal - 1} months')"
        start = (f"STRFTIME('%Y', {shifted}) || '-' || "
                 f"PRINTF('%02d', ((CAST(STRFTIME('%m', {shifted}) AS INTEGER) - 1) / 3) * 3 + 1) || '-01'"
                 if grain == "quarterly" else f"STRFTIME('%Y', {shifted}) || '-01-01'")
        return f"DATE({start}, '+{fiscal - 1} months')"
    if grain in formats:
        return f"STRFTIME('{formats[grain]}', {day})"
    if grain == "quarterly":
        return (f"STRFTIME('%Y', {day}) || '-Q' || "
                f"CAST((CAST(STRFTIME('%m', {day}) AS INTEGER) + 2) / 3 AS INTEGER)")
    return None


def month_shift(day, months):
    year, month = divmod(day.year * 12 + day.month - 1 + months, 12)
    return date(year, month + 1, min(day.day, calendar.monthrange(year, month + 1)[1]))


def period_bounds(day, grain, contract=None):
    fiscal = contract.fiscal_year_start_month if contract else 1
    if grain == "daily":
        return day, day
    if grain == "weekly":
        start = day - timedelta(days=day.weekday())
        return start, start + timedelta(days=6)
    start = day.replace(day=1)
    if grain == "monthly":
        return start, month_shift(start, 1) - timedelta(days=1)
    if grain in {"quarterly", "yearly"}:
        year_start = date(day.year - (day.month < fiscal), fiscal, 1)
        months = ((day.year - year_start.year) * 12 + day.month - fiscal)
        start = month_shift(year_start, (months // 3) * 3) if grain == "quarterly" else year_start
        return start, month_shift(start, 3 if grain == "quarterly" else 12) - timedelta(days=1)
    raise UnsupportedConstraint(f"unsupported_time_grain: {grain}")


def label_date(label):
    try:
        if "-Q" in label:
            year, quarter = label.split("-Q")
            return date(int(year), int(quarter) * 3 - 2, 1)
        return date.fromisoformat(label if len(label) == 10 else label + ("-01" if len(label) == 7 else "-01-01"))
    except ValueError as exc:
        raise UnsupportedConstraint(f"unsupported_calendar_period: {label}") from exc


def period_label(day, grain, contract=None):
    start, _ = period_bounds(day, grain, contract)
    if grain in {"daily", "weekly"} or (contract and contract.fiscal_year_start_month != 1 and grain in {"quarterly", "yearly"}):
        return start.isoformat()
    if grain == "monthly":
        return start.strftime("%Y-%m")
    if grain == "quarterly":
        return f"{start.year:04d}-Q{(start.month - 1) // 3 + 1}"
    return str(start.year)


def baseline_day(day, baseline, grain):
    if baseline in {"same_period_last_year", "previous_year"}:
        try:
            return day.replace(year=day.year - 1)
        except ValueError as exc:
            raise UnsupportedConstraint("unsupported_baseline: leap day has no same calendar day last year") from exc
    if baseline == "previous_month" and grain != "monthly":
        raise UnsupportedConstraint("unsupported_baseline: previous_month requires monthly grain")
    if baseline == "previous_quarter" and grain != "quarterly":
        raise UnsupportedConstraint("unsupported_baseline: previous_quarter requires quarterly grain")
    if baseline not in {"previous_period", "previous_month", "previous_quarter"}:
        raise UnsupportedConstraint(f"unsupported_baseline: {baseline}")
    if grain in {"daily", "weekly"}:
        return day - timedelta(days=7 if grain == "weekly" else 1)
    return month_shift(day, {"monthly": -1, "quarterly": -3, "yearly": -12}[grain])


def comparison_context(context, baseline, grain, contract=None):
    if not context or not baseline or not grain:
        return context
    ranges = list(context.ranges)
    for span in context.ranges:
        first, last = date.fromisoformat(span.start_date), date.fromisoformat(span.end_date)
        # Include the complete baseline buckets, never a clipped month-end shift.
        start, _ = period_bounds(baseline_day(period_bounds(first, grain, contract)[0], baseline, grain), grain, contract)
        _, end = period_bounds(baseline_day(period_bounds(last, grain, contract)[0], baseline, grain), grain, contract)
        ranges.append(span.model_copy(update={"expression": "comparison baseline:" + baseline,
                                              "start_date": start.isoformat(), "end_date": end.isoformat()}))
    return context.model_copy(update={"ranges": ranges})


def require_complete_period(label, grain, contract, requested_ranges=()):
    start, end = period_bounds(label_date(label), grain, contract)
    if not contract or not contract.require_complete_periods:
        return "unverified"
    if start < date.fromisoformat(contract.coverage_start) or end > date.fromisoformat(contract.complete_through):
        raise UnsupportedConstraint(f"incomplete_period: {label} is outside declared complete coverage")
    if requested_ranges and not any(date.fromisoformat(span[0]) <= start and date.fromisoformat(span[1]) >= end for span in requested_ranges):
        raise UnsupportedConstraint(f"incomplete_period: requested window clips {label}")
    return "complete"
