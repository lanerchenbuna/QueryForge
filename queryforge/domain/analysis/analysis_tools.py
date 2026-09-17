"""Deterministic analysis computations: period comparison, drill-down,
contribution decomposition, anomaly detection, and chart selection.

Step 11 separates *deciding* from *computing*: the model (or the planner)
chooses which analysis to run, while the business numbers are produced here by
small, declared, dependency-free functions.  Nothing in this module reads a
database, calls a model, reads a clock, or looks at the environment, so every
result is reproducible from its arguments alone.

Each computation returns a typed pydantic result carrying:

* ``method``          - the declared method that produced the numbers,
* ``parameters``      - the exact inputs, so a reader can recompute them,
* ``limitations``     - what the method does *not* support,
* ``undefined_reason`` - set whenever the requested quantity is not defined.

Two rules are enforced by construction rather than by documentation:

1. **No fabricated statistics.**  A percentage is only emitted when its
   denominator is a defined, non-zero, finite number.  A zero baseline yields an
   absolute change and an explicit ``zero_baseline`` state, never ``inf``/``NaN``
   or an invented ``100%``.
2. **No summation across non-additive inputs.**  Contributions are only
   decomposed for mutually exclusive, additive groups that the caller declares
   (``additive=True``, ``metric_kind="additive"``); ratio and distinct metrics
   are refused with a named reason, and ratios get a dedicated pooled estimator
   (:func:`combine_ratio`) instead.

Correlation is never presented as causation: every trend/contribution result
states that it decomposes arithmetic, it does not attribute causes.
"""

from __future__ import annotations

import math
from datetime import date as _date
from typing import Any, Iterable, Literal, Mapping, Sequence

from pydantic import BaseModel, ConfigDict, Field

#: Declared float tolerance.  Arithmetic on binary floats is only exact up to
#: this bound, so every "did these agree?" comparison in this module (and in the
#: tests that check row-order independence) uses it instead of ``==``.
FLOAT_TOLERANCE: float = 1e-9

#: Comparison methods this module is allowed to claim.
COMPARISON_METHODS: tuple[str, ...] = ("absolute_relative", "absolute_only")

#: Anomaly baselines this module implements.  No other method may be claimed.
ANOMALY_METHODS: tuple[str, ...] = ("baseline_deviation", "zscore")

SEASONALITY_KINDS: tuple[str, ...] = ("none", "weekly", "monthly")

MISSING_POLICIES: tuple[str, ...] = ("skip", "gap")

METRIC_KINDS: tuple[str, ...] = ("additive", "ratio", "distinct")

TIME_GRAINS: tuple[str, ...] = ("daily", "weekly", "monthly", "quarterly", "yearly")

CHART_TYPES: tuple[str, ...] = ("line", "bar", "metric", "table")

_MAD_SCALE = 1.4826  # median absolute deviation -> normal-consistent sigma


def numbers_close(left: float | None, right: float | None, *, tolerance: float = FLOAT_TOLERANCE) -> bool:
    """Declared float comparison used for residual checks and gold values."""

    if left is None or right is None:
        return left is None and right is None
    if not (math.isfinite(left) and math.isfinite(right)):
        return left == right
    return abs(left - right) <= tolerance


# --------------------------------------------------------------------- inputs


def require_consistent_units(units: Iterable[str | None]) -> str | None:
    """Return the single unit of ``units`` or refuse a mixed-unit merge.

    Step 11-E1: two numbers from different currencies/units may not be added,
    compared, or charted together.  ``None`` values are "undeclared" and are
    ignored; mixing a declared unit with an undeclared one is allowed but the
    caller keeps responsibility (the result stays labelled with the declared
    unit).
    """

    return _require_single("unit", units)


def require_consistent_versions(versions: Iterable[str | None]) -> str | None:
    """Return the single data/model version of ``versions`` or refuse a merge."""

    return _require_single("version", versions)


def require_consistent_grain(grains: Iterable[str | None]) -> str | None:
    """Return the single time grain of ``grains`` or refuse a mixed-grain merge."""

    return _require_single("grain", grains)


def _require_single(label: str, values: Iterable[str | None]) -> str | None:
    declared = sorted({str(value).strip() for value in values if value is not None and str(value).strip()})
    if len(declared) > 1:
        raise ValueError(
            f"mixed_{label}s: refusing to combine inputs with different {label}s "
            f"({', '.join(declared)}); provide a converted/single-{label} input or "
            "report the comparison explicitly as a cross-"
            f"{label} comparison instead of merging them."
        )
    return declared[0] if declared else None


# ----------------------------------------------------------------- comparison


class PeriodComparison(BaseModel):
    """Absolute and relative change between a current and a baseline window."""

    model_config = ConfigDict(extra="forbid")

    method: str = "absolute_relative"
    state: Literal[
        "ok",
        "zero_baseline",
        "both_zero",
        "missing_value",
        "undefined_input",
        "method_limited",
    ]
    label: str | None = None
    current: float | None = None
    baseline: float | None = None
    delta: float | None = None
    relative_change: float | None = None
    percent_change: float | None = None
    undefined_reason: str | None = None
    parameters: dict[str, Any] = Field(default_factory=dict)
    limitations: list[str] = Field(default_factory=list)

    @property
    def defined(self) -> bool:
        """True when at least the absolute change is a real number."""

        return self.delta is not None

    @property
    def relative_defined(self) -> bool:
        return self.relative_change is not None


def compare_periods(
    current: float | int | None,
    baseline: float | int | None,
    *,
    label: str | None = None,
    method: str = "absolute_relative",
) -> PeriodComparison:
    """Compare two windows.

    ``delta`` is ``current - baseline``.  ``relative_change`` is
    ``delta / |baseline|`` (so its sign always matches ``delta``, including for a
    negative baseline) and ``percent_change`` is that number times 100.  A
    relative change is only emitted when the baseline is a non-zero finite
    number; a zero baseline yields the ``zero_baseline``/``both_zero`` state with
    ``relative_change=None``, never a fabricated percentage.
    """

    if method not in COMPARISON_METHODS:
        raise ValueError(
            f"unsupported comparison method {method!r}; declared methods are "
            f"{', '.join(COMPARISON_METHODS)}"
        )
    current_value = _nullable_number(current, field_name="current")
    baseline_value = _nullable_number(baseline, field_name="baseline")
    parameters: dict[str, Any] = {
        "current": current_value,
        "baseline": baseline_value,
        "method": method,
        "relative_denominator": "abs(baseline)",
    }
    limitations = [
        "Describes only the two supplied windows: it is not a trend estimate, a "
        "significance test, or a causal attribution.",
        "The relative change divides by |baseline|, so it is a symmetric scale "
        "factor, not a compounded growth rate.",
    ]

    if current_value is None or baseline_value is None:
        missing = [
            name
            for name, value in (("current", current_value), ("baseline", baseline_value))
            if value is None
        ]
        parameters["missing"] = missing
        return PeriodComparison(
            method=method,
            state="missing_value",
            label=label,
            current=current_value,
            baseline=baseline_value,
            undefined_reason="missing_value",
            parameters=parameters,
            limitations=limitations
            + ["A window without rows (or with an unaggregated NULL) has no value; "
               "the change is undefined rather than zero."],
        )
    if not (math.isfinite(current_value) and math.isfinite(baseline_value)):
        parameters["non_finite"] = [
            name
            for name, value in (("current", current_value), ("baseline", baseline_value))
            if not math.isfinite(value)
        ]
        return PeriodComparison(
            method=method,
            state="undefined_input",
            label=label,
            current=current_value,
            baseline=baseline_value,
            undefined_reason="non_finite_input",
            parameters=parameters,
            limitations=limitations
            + ["inf/NaN inputs cannot produce a comparable change; they usually "
               "signal a division by zero inside the metric definition."],
        )

    delta = current_value - baseline_value
    if baseline_value == 0 and current_value == 0:
        return PeriodComparison(
            method=method,
            state="both_zero",
            label=label,
            current=current_value,
            baseline=baseline_value,
            delta=delta,
            undefined_reason="both_zero",
            parameters=parameters,
            limitations=limitations
            + ["Both windows are zero: there is neither an absolute nor a relative "
               "change, and '0%' would imply an observed baseline."],
        )
    if baseline_value == 0:
        return PeriodComparison(
            method=method,
            state="zero_baseline",
            label=label,
            current=current_value,
            baseline=baseline_value,
            delta=delta,
            undefined_reason="zero_baseline",
            parameters=parameters,
            limitations=limitations
            + ["The baseline is zero, so a relative/percentage change is undefined; "
               "only the absolute change is reported."],
        )
    if method == "absolute_only":
        return PeriodComparison(
            method=method,
            state="method_limited",
            label=label,
            current=current_value,
            baseline=baseline_value,
            delta=delta,
            undefined_reason="method_does_not_define_relative",
            parameters=parameters,
            limitations=limitations
            + ["The declared method 'absolute_only' does not define a relative change."],
        )

    relative = delta / abs(baseline_value)
    return PeriodComparison(
        method=method,
        state="ok",
        label=label,
        current=current_value,
        baseline=baseline_value,
        delta=delta,
        relative_change=relative,
        percent_change=relative * 100.0,
        parameters=parameters,
        limitations=limitations,
    )


# ------------------------------------------------------------------ drill down


class DrillDownBucket(BaseModel):
    """One category of a drill-down result (kept bucket or the ``others`` tail)."""

    model_config = ConfigDict(extra="forbid")

    category: str
    value: float
    share: float | None = None


class DrillDown(BaseModel):
    """Bounded top-N breakdown with an explicit, auditable ``others`` bucket."""

    model_config = ConfigDict(extra="forbid")

    method: str = "top_n_with_others"
    parameters: dict[str, Any] = Field(default_factory=dict)
    dimension: str | None = None
    buckets: list[DrillDownBucket] = Field(default_factory=list)
    others: DrillDownBucket | None = None
    others_reasons: dict[str, list[str]] = Field(default_factory=dict)
    kept_count: int = 0
    others_category_count: int = 0
    total_value: float | None = None
    kept_value: float = 0.0
    coverage: float | None = None
    coverage_basis: Literal["declared_total", "bucket_sum", "none"] = "none"
    truncated: bool = False
    undefined_reason: str | None = None
    limitations: list[str] = Field(default_factory=list)


def drill_down(
    buckets: Sequence[Mapping[str, Any]],
    *,
    total: float | int | None = None,
    max_categories: int = 10,
    min_sample: float | int | None = None,
    dimension: str | None = None,
) -> DrillDown:
    """Rank categories, keep the top ``max_categories``, aggregate the tail.

    The tail (plus every category below ``min_sample``) becomes one explicit
    ``others`` bucket that records its value, how many categories it hides, and
    why each category was grouped.  ``coverage`` is kept value over the declared
    total when one is supplied, otherwise over the sum of the supplied buckets,
    so a caller can always see how much of the metric the visible rows explain.
    Buckets are sorted by value desc (category name breaks ties) which makes the
    result independent of input row order.
    """

    if not isinstance(max_categories, int) or isinstance(max_categories, bool) or max_categories < 1:
        raise ValueError(
            f"max_categories must be a positive integer, got {max_categories!r}"
        )
    min_sample_value = _nullable_number(min_sample, field_name="min_sample")
    if min_sample_value is not None and min_sample_value < 0:
        raise ValueError("min_sample must be zero or greater")
    total_value = _nullable_number(total, field_name="total")

    normalized: list[tuple[str, float]] = []
    null_categories: list[str] = []
    for index, bucket in enumerate(buckets):
        category, value = _bucket_entry(bucket, index=index, keys=("value",), tool="drill_down")
        if value is None:
            value = 0.0
            null_categories.append(category)
        normalized.append((category, value))
    normalized.sort(key=lambda item: (-item[1], item[0]))

    parameters: dict[str, Any] = {
        "max_categories": max_categories,
        "min_sample": min_sample_value,
        "declared_total": total_value,
        "bucket_count": len(normalized),
    }
    if null_categories:
        parameters["null_values_treated_as_zero"] = sorted(null_categories)
    limitations = [
        "The tail is aggregated into one 'others' bucket; individual tail "
        "categories are not comparable to the visible ones.",
        "Ranking uses the bucket values as supplied; it does not test whether a "
        "difference between two categories is significant.",
    ]
    if null_categories:
        limitations.append(
            "NULL bucket values were treated as 0 for ranking; a NULL metric slice "
            "is not the same evidence as a measured zero."
        )

    bucket_sum = math.fsum(value for _, value in normalized)
    eligible: list[tuple[str, float]] = []
    below_min: list[tuple[str, float]] = []
    for category, value in normalized:
        if min_sample_value is not None and value < min_sample_value:
            below_min.append((category, value))
        else:
            eligible.append((category, value))

    kept = eligible[:max_categories]
    tail = eligible[max_categories:]
    others_reasons: dict[str, list[str]] = {}
    if tail:
        others_reasons["tail"] = [category for category, _ in tail]
    if below_min:
        others_reasons["below_min_sample"] = [category for category, _ in below_min]

    kept_value = math.fsum(value for _, value in kept)
    denominator = total_value if total_value is not None else bucket_sum
    basis: Literal["declared_total", "bucket_sum", "none"] = (
        "declared_total" if total_value is not None else "bucket_sum"
    )
    if denominator == 0:
        coverage = None
        basis = "none"
        limitations.append(
            "Coverage is undefined because the reference total is zero."
        )
    else:
        coverage = kept_value / denominator

    others: DrillDownBucket | None = None
    excluded = tail + below_min
    if excluded:
        others_value = math.fsum(value for _, value in excluded)
        others = DrillDownBucket(
            category="others",
            value=others_value,
            share=(others_value / denominator) if denominator else None,
        )

    result = DrillDown(
        parameters=parameters,
        dimension=dimension,
        buckets=[
            DrillDownBucket(
                category=category,
                value=value,
                share=(value / denominator) if denominator else None,
            )
            for category, value in kept
        ],
        others=others,
        others_reasons=others_reasons,
        kept_count=len(kept),
        others_category_count=len(excluded),
        total_value=total_value if total_value is not None else bucket_sum,
        kept_value=kept_value,
        coverage=coverage,
        coverage_basis=basis,
        truncated=bool(excluded),
        undefined_reason=None if normalized else "empty_buckets",
        limitations=limitations
        + [
            "Coverage is kept value divided by the declared total when supplied, "
            "otherwise by the sum of the supplied buckets; if the input itself was "
            "truncated, coverage is only a lower bound."
        ],
    )
    if not normalized:
        result.limitations.append(
            "No buckets were supplied, so no ranking exists; an empty breakdown is "
            "not evidence that the metric is zero."
        )
    return result


# ---------------------------------------------------------------- contribution


class ContributionItem(BaseModel):
    """One additive group's share of the total change."""

    model_config = ConfigDict(extra="forbid")

    category: str
    current: float
    baseline: float
    delta: float
    share: float | None = None
    share_pct: float | None = None
    direction: Literal["increase", "decrease", "flat"] = "flat"
    offsetting: bool = False


class ContributionBreakdown(BaseModel):
    """Reconciled decomposition of a total change into additive groups."""

    model_config = ConfigDict(extra="forbid")

    method: str = "delta_share_of_total_change"
    metric_kind: Literal["additive", "ratio", "distinct"] = "additive"
    additive: bool = True
    parameters: dict[str, Any] = Field(default_factory=dict)
    contributions: list[ContributionItem] = Field(default_factory=list)
    total_delta: float | None = None
    computed_total_delta: float | None = None
    total_delta_source: Literal["declared", "computed", "none"] = "none"
    residual: float | None = None
    residual_explained: bool = False
    tolerance: float = FLOAT_TOLERANCE
    share_undefined_reason: str | None = None
    undefined_reason: str | None = None
    limitations: list[str] = Field(default_factory=list)


def contribution_breakdown(
    buckets: Sequence[Mapping[str, Any]],
    *,
    expected_total_delta: float | int | None = None,
    tolerance: float = FLOAT_TOLERANCE,
    additive: bool = True,
    metric_kind: Literal["additive", "ratio", "distinct"] = "additive",
) -> ContributionBreakdown:
    """Split a total change into mutually exclusive, additive groups.

    Each item's ``delta`` is ``current - baseline`` and its ``share`` is
    ``delta / total_delta`` (so a group moving against the total has a negative
    share).  When the caller supplies ``expected_total_delta`` - normally a
    separately computed total from a *different* query - the residual
    (``sum(deltas) - total_delta``) and ``residual_explained`` show whether the
    decomposition reconciles.

    Inputs that are not additive are refused instead of summed: overlapping
    groups, ratio metrics, and distinct counts.  Ratio metrics belong to
    :func:`combine_ratio`, and distinct counts across overlapping groups cannot
    be decomposed at all.
    """

    if metric_kind not in METRIC_KINDS:
        raise ValueError(
            f"unsupported metric_kind {metric_kind!r}; declared kinds are "
            f"{', '.join(METRIC_KINDS)}"
        )
    if tolerance < 0:
        raise ValueError("tolerance must be zero or greater")
    declared_total = _nullable_number(expected_total_delta, field_name="expected_total_delta")
    base_parameters: dict[str, Any] = {
        "metric_kind": metric_kind,
        "additive": bool(additive),
        "tolerance": tolerance,
        "declared_total_delta": declared_total,
        "bucket_count": len(buckets),
    }
    common_limitations = [
        "A contribution is an arithmetic decomposition of the total change, not a "
        "causal attribution: 'channel X contributed -30' does not mean X caused "
        "the decline.",
        "Only mutually exclusive, additive groups over the same window, unit and "
        "version may be decomposed; the caller declares additivity.",
    ]

    if not additive:
        return ContributionBreakdown(
            metric_kind=metric_kind,
            additive=False,
            parameters=base_parameters,
            tolerance=tolerance,
            undefined_reason="non_additive_input",
            limitations=common_limitations
            + ["The caller declared additive=False (overlapping or hierarchical "
               "groups), so the deltas were not summed."],
        )
    if metric_kind != "additive":
        reason = f"{metric_kind}_not_additive"
        return ContributionBreakdown(
            metric_kind=metric_kind,
            additive=True,
            parameters=base_parameters,
            tolerance=tolerance,
            undefined_reason=reason,
            limitations=common_limitations
            + [
                f"A {metric_kind} metric cannot be decomposed by summation: "
                + (
                    "group ratios must be pooled from their real numerator and "
                    "denominator (combine_ratio), never added."
                    if metric_kind == "ratio"
                    else "distinct counts of overlapping groups double-count shared "
                    "members, so only a non-overlapping partition may be summed."
                )
            ],
        )

    normalized: list[tuple[str, float | None, float | None]] = []
    for index, bucket in enumerate(buckets):
        category, current, baseline = _bucket_entry(
            bucket, index=index, keys=("current", "baseline"), tool="calculate_contribution"
        )
        normalized.append((category, current, baseline))

    if not normalized:
        return ContributionBreakdown(
            metric_kind=metric_kind,
            parameters=base_parameters,
            tolerance=tolerance,
            undefined_reason="empty_input",
            limitations=common_limitations + ["No groups were supplied."],
        )
    missing = sorted(category for category, current, baseline in normalized if current is None or baseline is None)
    non_finite = sorted(
        category
        for category, current, baseline in normalized
        if (current is not None and not math.isfinite(current))
        or (baseline is not None and not math.isfinite(baseline))
    )
    if missing or non_finite:
        base_parameters["missing_categories"] = missing
        base_parameters["non_finite_categories"] = non_finite
        return ContributionBreakdown(
            metric_kind=metric_kind,
            parameters=base_parameters,
            tolerance=tolerance,
            undefined_reason="missing_value" if missing else "non_finite_input",
            limitations=common_limitations
            + ["A group without a defined current/baseline value leaves the total "
               "change unreconciled, so no shares were computed."],
        )

    items = [
        (category, float(current), float(baseline))  # type: ignore[arg-type]
        for category, current, baseline in normalized
    ]
    items.sort(key=lambda item: (-abs(item[1] - item[2]), item[0]))
    deltas = [(category, current - baseline) for category, current, baseline in items]
    computed_total = math.fsum(delta for _, delta in deltas)
    total_delta = declared_total if declared_total is not None else computed_total
    residual = computed_total - total_delta
    total_direction = 0.0 if total_delta == 0 else math.copysign(1.0, total_delta)
    shares_defined = total_delta != 0
    contributions = [
        ContributionItem(
            category=category,
            current=current,
            baseline=baseline,
            delta=current - baseline,
            share=((current - baseline) / total_delta) if shares_defined else None,
            share_pct=(((current - baseline) / total_delta) * 100.0) if shares_defined else None,
            direction=(
                "flat" if current - baseline == 0 else ("increase" if current - baseline > 0 else "decrease")
            ),
            offsetting=(
                total_direction != 0
                and (current - baseline) != 0
                and math.copysign(1.0, current - baseline) != total_direction
            ),
        )
        for category, current, baseline in items
    ]
    limitations = list(common_limitations)
    if shares_defined:
        limitations.append(
            "Shares are delta_i / total_delta; they sum to 1 when the decomposition "
            "reconciles, and a group moving against the total shows a negative share."
        )
    else:
        limitations.append(
            "The total change is zero, so per-group shares of change are undefined "
            "(each group still reports its absolute delta)."
        )
    if not numbers_close(residual, 0.0, tolerance=tolerance):
        limitations.append(
            "The group deltas do not reconcile with the reported total change "
            f"(residual={residual!r}); check for overlapping groups, an omitted "
            "group, or a truncated input."
        )
    return ContributionBreakdown(
        metric_kind=metric_kind,
        parameters=base_parameters,
        contributions=contributions,
        total_delta=total_delta,
        computed_total_delta=computed_total,
        total_delta_source="declared" if declared_total is not None else "computed",
        residual=residual,
        residual_explained=numbers_close(residual, 0.0, tolerance=tolerance),
        tolerance=tolerance,
        share_undefined_reason=None if shares_defined else "zero_total_delta",
        limitations=limitations,
    )


class RatioBucket(BaseModel):
    """One group's numerator/denominator pair."""

    model_config = ConfigDict(extra="forbid")

    category: str
    numerator: float
    denominator: float
    ratio: float | None = None


class RatioCombination(BaseModel):
    """Pooled (weighted) ratio for ratio metrics."""

    model_config = ConfigDict(extra="forbid")

    method: str = "aggregate_numerator_over_denominator"
    parameters: dict[str, Any] = Field(default_factory=dict)
    buckets: list[RatioBucket] = Field(default_factory=list)
    total_numerator: float | None = None
    total_denominator: float | None = None
    ratio: float | None = None
    unweighted_mean_ratio: float | None = None
    undefined_reason: str | None = None
    limitations: list[str] = Field(default_factory=list)


def combine_ratio(
    buckets: Sequence[Mapping[str, Any]],
    *,
    method: str = "aggregate_numerator_over_denominator",
) -> RatioCombination:
    """Pool a ratio metric from real numerators and denominators (11-B2).

    A ratio is not additive: summing group ratios, or averaging them
    unweighted, gives a number that is not the ratio of the whole.  This
    function computes ``sum(numerator) / sum(denominator)`` and *also* reports
    the unweighted mean of the group ratios in a clearly separate field, so a
    caller can see how far that tempting shortcut would have been from the
    pooled value.  Only the pooled value may be quoted as the overall ratio.
    """

    if method != "aggregate_numerator_over_denominator":
        raise ValueError(
            "unsupported ratio method "
            f"{method!r}; declared method is 'aggregate_numerator_over_denominator' "
            "(an unweighted mean of ratios is not the overall ratio)"
        )
    normalized: list[tuple[str, float | None, float | None]] = []
    for index, bucket in enumerate(buckets):
        category, numerator, denominator = _bucket_entry(
            bucket, index=index, keys=("numerator", "denominator"), tool="combine_ratio"
        )
        normalized.append((category, numerator, denominator))
    normalized.sort(key=lambda item: item[0])
    limitations = [
        "The pooled ratio is the only value that describes the whole population; "
        "the unweighted mean of group ratios is reported for contrast only and "
        "must not be quoted as the overall ratio.",
        "Pooling assumes numerator and denominator cover the same population and "
        "the same unit/version.",
    ]
    if not normalized:
        return RatioCombination(parameters={"method": method}, undefined_reason="empty_input", limitations=limitations)
    undefined = next(
        (
            category
            for category, numerator, denominator in normalized
            if numerator is None or denominator is None
        ),
        None,
    )
    if undefined is not None:
        return RatioCombination(
            parameters={"method": method, "undefined_category": undefined, "bucket_count": len(normalized)},
            undefined_reason="missing_value",
            limitations=limitations
            + ["A group without a defined numerator/denominator cannot be pooled."],
        )
    total_numerator = math.fsum(float(numerator) for _, numerator, _ in normalized)  # type: ignore[arg-type]
    total_denominator = math.fsum(float(denominator) for _, _, denominator in normalized)  # type: ignore[arg-type]
    group_ratios = [
        (float(numerator) / float(denominator)) if float(denominator) != 0 else None
        for _, numerator, denominator in normalized
    ]
    defined_ratios = [ratio for ratio in group_ratios if ratio is not None]
    unweighted = (math.fsum(defined_ratios) / len(defined_ratios)) if defined_ratios else None
    ratio = (total_numerator / total_denominator) if total_denominator != 0 else None
    return RatioCombination(
        parameters={"method": method, "bucket_count": len(normalized)},
        buckets=[
            RatioBucket(category=category, numerator=float(numerator), denominator=float(denominator), ratio=group_ratio)  # type: ignore[arg-type]
            for (category, numerator, denominator), group_ratio in zip(normalized, group_ratios)
        ],
        total_numerator=total_numerator,
        total_denominator=total_denominator,
        ratio=ratio,
        unweighted_mean_ratio=unweighted,
        undefined_reason=None if ratio is not None else "zero_denominator",
        limitations=limitations
        + ([] if ratio is not None else ["The pooled denominator is zero, so the ratio is undefined."]),
    )


# -------------------------------------------------------------------- anomaly


class AnomalyPoint(BaseModel):
    """One point of the analysed series with its expected value and score."""

    model_config = ConfigDict(extra="forbid")

    period: str
    value: float | None = None
    expected: float | None = None
    deviation: float | None = None
    score: float | None = None
    is_anomaly: bool = False
    direction: Literal["above", "below", "flat"] | None = None
    gap_filled: bool = False
    reason: str | None = None


class AnomalyReport(BaseModel):
    """Explained anomaly scan over one period series."""

    model_config = ConfigDict(extra="forbid")

    method: str = "baseline_deviation"
    method_description: str = ""
    state: Literal["ok", "constant_series", "insufficient_data", "undefined_input"] = "ok"
    parameters: dict[str, Any] = Field(default_factory=dict)
    points: list[AnomalyPoint] = Field(default_factory=list)
    anomalies: list[AnomalyPoint] = Field(default_factory=list)
    baseline_center: float | None = None
    baseline_scale: float | None = None
    scale_method: str | None = None
    baseline_n: int = 0
    observed_n: int = 0
    threshold: float = 2.0
    seasonality: str = "none"
    seasonality_applied: bool = False
    seasonal_medians: dict[str, float] = Field(default_factory=dict)
    missing_policy: str = "skip"
    undefined_reason: str | None = None
    limitations: list[str] = Field(default_factory=list)


_METHOD_DESCRIPTIONS = {
    "baseline_deviation": (
        "robust baseline: center = median(series), scale = 1.4826 * MAD "
        "(mean absolute deviation from the median when MAD is 0); score = "
        "(value - center) / scale, flagged when |score| >= threshold"
    ),
    "zscore": (
        "mean/std baseline: center = arithmetic mean, scale = population standard "
        "deviation (divided by n); score = (value - center) / scale, flagged when "
        "|score| >= threshold"
    ),
}

_ANOMALY_LIMITATIONS = [
    "A flagged point is a deviation from the declared baseline, not a proven "
    "incident; no causal claim follows from it.",
    "The threshold is a fixed multiple of the baseline scale, not a significance "
    "test: no p-value, confidence interval, or false-positive rate is computed.",
    "Only a level shift / spike against the declared baseline is modelled; trend "
    "changes, multiple breakpoints, and correlated noise are not.",
]


def detect_anomaly(
    series: Sequence[Mapping[str, Any]],
    *,
    method: str = "baseline_deviation",
    min_points: int = 6,
    seasonality: Literal["none", "weekly", "monthly"] = "none",
    missing: Literal["skip", "gap"] = "skip",
    threshold: float = 2.0,
) -> AnomalyReport:
    """Scan a period series for level deviations from a declared baseline.

    Declared method (see ``method_description`` on the result): the center is the
    median (``baseline_deviation``) or the mean (``zscore``); the scale is
    ``1.4826 * MAD`` for the robust method (falling back to the mean absolute
    deviation from the median when MAD is exactly 0) or the population standard
    deviation for ``zscore``.  A point is flagged when
    ``|value - center| / scale >= threshold``.

    ``seasonality`` removes a per-season median (weekday for ``weekly``, calendar
    month for ``monthly``) from an ISO-dated series before the baseline is
    estimated; seasons observed once are left unadjusted and reported as such.
    ``missing`` decides how a NULL observation enters the scan: ``skip`` estimates
    the baseline from observed points only, ``gap`` forward-fills the last
    observed value before estimating it (a filled point is never flagged as an
    anomaly, because no observation supports it).
    """

    if method not in ANOMALY_METHODS:
        raise ValueError(
            f"unsupported anomaly method {method!r}; declared methods are "
            f"{', '.join(ANOMALY_METHODS)}"
        )
    if not isinstance(min_points, int) or isinstance(min_points, bool) or min_points < 2:
        raise ValueError("min_points must be an integer >= 2")
    if seasonality not in SEASONALITY_KINDS:
        raise ValueError(
            f"unsupported seasonality {seasonality!r}; declared kinds are "
            f"{', '.join(SEASONALITY_KINDS)}"
        )
    if missing not in MISSING_POLICIES:
        raise ValueError(
            f"unsupported missing policy {missing!r}; declared policies are "
            f"{', '.join(MISSING_POLICIES)}"
        )
    if threshold < 0:
        raise ValueError("threshold must be zero or greater")

    points: list[AnomalyPoint] = []
    for index, item in enumerate(series):
        period, value = _series_entry(item, index=index)
        if value is not None and not math.isfinite(value):
            points.append(
                AnomalyPoint(period=period, value=None, reason="non_finite_value")
            )
            continue
        points.append(AnomalyPoint(period=period, value=value, reason=None if value is not None else "missing_value"))

    parameters: dict[str, Any] = {
        "method": method,
        "min_points": min_points,
        "seasonality": seasonality,
        "missing": missing,
        "threshold": threshold,
        "point_count": len(points),
        "flag_rule": "abs(score) >= threshold",
    }
    report = AnomalyReport(
        method=method,
        method_description=_METHOD_DESCRIPTIONS[method],
        parameters=parameters,
        points=points,
        threshold=threshold,
        seasonality=seasonality,
        missing_policy=missing,
        limitations=list(_ANOMALY_LIMITATIONS),
        observed_n=sum(1 for point in points if point.value is not None),
    )

    if not points:
        return report.model_copy(
            update={
                "state": "undefined_input",
                "undefined_reason": "empty_series",
                "limitations": report.limitations
                + ["No series was supplied; no baseline and no anomaly exist."],
            }
        )
    if report.observed_n < min_points:
        return report.model_copy(
            update={
                "state": "insufficient_data",
                "undefined_reason": "insufficient_data",
                "limitations": report.limitations
                + [
                    f"Only {report.observed_n} observed point(s) for a declared "
                    f"minimum of {min_points}: nothing is flagged instead of "
                    "estimating a baseline from too little data."
                ],
            }
        )

    adjustment: dict[int, float] = {index: 0.0 for index in range(len(points))}
    seasonal_medians: dict[str, float] = {}
    if seasonality != "none":
        parsed = [_parse_iso_date(point.period) for point in points]
        if any(value is None for value in parsed):
            return report.model_copy(
                update={
                    "state": "undefined_input",
                    "undefined_reason": "seasonality_requires_iso_dates",
                    "limitations": report.limitations
                    + [
                        "Seasonal adjustment needs ISO 'YYYY-MM-DD' period labels; "
                        "coarser labels cannot identify a weekday/month."
                    ],
                }
            )
        groups: dict[str, list[float]] = {}
        for index, (point, day) in enumerate(zip(points, parsed)):
            if point.value is None or day is None:
                continue
            groups.setdefault(_season_key(day, seasonality), []).append(point.value)
        singleton_seasons: list[str] = []
        for key, values in groups.items():
            if len(values) >= 2:
                median = _median(values)
                seasonal_medians[key] = median
                for index, (point, day) in enumerate(zip(points, parsed)):
                    if day is not None and _season_key(day, seasonality) == key and point.value is not None:
                        adjustment[index] = median
            else:
                singleton_seasons.append(key)
        report = report.model_copy(update={"seasonality_applied": True, "seasonal_medians": seasonal_medians})
        if singleton_seasons:
            report = report.model_copy(
                update={
                    "limitations": report.limitations
                    + [
                        "Season(s) "
                        + ", ".join(sorted(singleton_seasons))
                        + " were observed once and are left unadjusted."
                    ]
                }
            )

    adjusted: list[float | None] = [
        (point.value - adjustment[index]) if point.value is not None else None
        for index, point in enumerate(points)
    ]
    if missing == "gap":
        carried: float | None = None
        filled: list[float | None] = []
        for index, value in enumerate(adjusted):
            if value is None:
                filled.append(carried)
                if carried is not None:
                    points[index] = points[index].model_copy(
                        update={"gap_filled": True, "reason": "missing_value_gap_filled"}
                    )
            else:
                carried = value
                filled.append(value)
        baseline_series = filled
    else:
        baseline_series = adjusted
    used = [value for value in baseline_series if value is not None]

    if method == "zscore":
        center = math.fsum(used) / len(used)
        variance = math.fsum((value - center) ** 2 for value in used) / len(used)
        scale = math.sqrt(max(0.0, variance))
        scale_method = "population_standard_deviation"
        if scale == 0:
            scale = 0.0
            scale_method = "constant_series"
    else:
        center = _median(used)
        deviations = [abs(value - center) for value in used]
        mad = _median(deviations)
        if mad > 0:
            scale = mad * _MAD_SCALE
            scale_method = "median_absolute_deviation_x1.4826"
        else:
            mean_abs = math.fsum(deviations) / len(deviations)
            scale = mean_abs
            scale_method = "mean_absolute_deviation_from_median" if mean_abs > 0 else "constant_series"

    report = report.model_copy(
        update={
            "baseline_center": center,
            "baseline_scale": scale,
            "scale_method": scale_method,
            "baseline_n": len(used),
            "points": points,
        }
    )

    constant = scale == 0
    scored_points: list[AnomalyPoint] = []
    for index, point in enumerate(points):
        raw = baseline_series[index]
        if raw is None:
            scored_points.append(
                point.model_copy(update={"expected": None, "deviation": None, "score": None})
            )
            continue
        expected = raw + adjustment[index]
        deviation = raw - center
        score = 0.0 if constant else deviation / scale
        flagged = (
            not constant
            and not point.gap_filled
            and abs(score) >= threshold
        )
        scored_points.append(
            point.model_copy(
                update={
                    "expected": expected,
                    "deviation": deviation,
                    "score": score,
                    "is_anomaly": flagged,
                    "direction": (
                        "flat"
                        if deviation == 0
                        else ("above" if deviation > 0 else "below")
                    ),
                    "reason": point.reason,
                }
            )
        )
    anomalies = [point for point in scored_points if point.is_anomaly]

    state: Literal["ok", "constant_series", "insufficient_data", "undefined_input"] = "ok"
    undefined_reason: str | None = None
    limitations = list(report.limitations)
    if constant:
        state = "constant_series"
        undefined_reason = "constant_series"
        limitations.append(
            "The series is constant (baseline scale 0): every score is 0 and no "
            "point is flagged, because 'deviating from a flat baseline' is undefined."
        )
    return report.model_copy(
        update={
            "points": scored_points,
            "anomalies": anomalies,
            "state": state,
            "undefined_reason": undefined_reason,
            "limitations": limitations
            + [
                f"{len(anomalies)} of {report.observed_n} observed point(s) flagged "
                f"with |score| >= {threshold}."
            ],
        }
    )


# --------------------------------------------------------------------- charts


class ChartSpec(BaseModel):
    """Deterministic chart/table decision for one result shape."""

    model_config = ConfigDict(extra="forbid")

    method: str = "declared_chart_selection"
    chart_type: Literal["line", "bar", "metric", "table"] = "table"
    reason: str = ""
    rationale: str = ""
    grain: str | None = None
    metric_kind: str = "additive"
    fields: dict[str, str] = Field(default_factory=dict)
    encoding: dict[str, str] = Field(default_factory=dict)
    vega_lite_spec: dict[str, Any] | None = None
    table: dict[str, Any] | None = None
    row_count: int = 0
    point_count: int = 0
    parameters: dict[str, Any] = Field(default_factory=dict)
    undefined_reason: str | None = None
    limitations: list[str] = Field(default_factory=list)


def build_chart(
    rows: Sequence[Sequence[Any]],
    columns: Sequence[str],
    *,
    metric_kind: Literal["additive", "ratio", "distinct"] = "additive",
    grain: str | None = None,
    chart_type: str | None = None,
) -> ChartSpec:
    """Pick a chart only when the data justifies one, otherwise a table.

    Declared selection rules (in order): no columns / no rows / no numeric
    metric column / an all-NULL metric / negative values on a ratio-or-distinct
    metric / an explicit ``table`` request / a single scalar / fewer than two
    time points all fall back to a table or a single-value "metric" card, each
    with a named ``reason``.  A time grain with at least two comparable points
    becomes a line; a categorical breakdown with at least two rows becomes a bar
    (never stacked, because ratio and distinct metrics are not additive).
    Field names are sanitized so a column like ``"Total Revenue (USD)"`` cannot
    break the rendered spec.
    """

    if metric_kind not in METRIC_KINDS:
        raise ValueError(
            f"unsupported metric_kind {metric_kind!r}; declared kinds are "
            f"{', '.join(METRIC_KINDS)}"
        )
    if chart_type is not None and chart_type not in CHART_TYPES:
        raise ValueError(
            f"unsupported chart_type {chart_type!r}; declared types are "
            f"{', '.join(CHART_TYPES)}"
        )
    column_names = [str(name) for name in columns]
    row_values = [list(row) for row in rows]
    fields = _safe_field_names(column_names)
    parameters: dict[str, Any] = {
        "metric_kind": metric_kind,
        "grain": grain,
        "requested_chart_type": chart_type,
        "column_count": len(column_names),
        "row_count": len(row_values),
    }
    limitations = [
        "The chart is a presentation of the supplied rows only; it inherits every "
        "limitation of the query that produced them (truncation, grain, NULLs).",
        "No axis is scaled, aggregated, or interpolated here: points are plotted as "
        "given, without smoothing or trend fitting; a NULL metric value is omitted "
        "rather than plotted as 0.",
    ]
    table = {"columns": list(column_names), "rows": row_values}

    def as_table(reason: str, rationale: str, *, undefined: str | None = None, extra: Sequence[str] = ()) -> ChartSpec:
        return ChartSpec(
            chart_type="table",
            reason=reason,
            rationale=rationale,
            grain=grain,
            metric_kind=metric_kind,
            fields=fields,
            table=table,
            row_count=len(row_values),
            parameters=parameters,
            undefined_reason=undefined,
            limitations=limitations + list(extra),
        )

    if not column_names:
        return as_table(
            "no_columns",
            "No column names were supplied, so no field can be referenced.",
            undefined="no_columns",
        )
    if not row_values:
        return as_table(
            "empty_result",
            "The result has no rows; an empty chart would imply a trend that was "
            "never observed.",
            undefined="empty_result",
        )

    metric_index = _metric_column_index(column_names, row_values)
    if metric_index is None:
        return as_table(
            "no_numeric_metric",
            "No column contains numeric values, so there is nothing to plot.",
            undefined="no_numeric_metric",
        )
    metric_values = [
        _optional_float(row[metric_index]) if len(row) > metric_index else None
        for row in row_values
    ]
    defined = [value for value in metric_values if value is not None and math.isfinite(value)]
    parameters["metric_column"] = column_names[metric_index]
    parameters["defined_points"] = len(defined)
    if not defined:
        return as_table(
            "metric_all_null",
            "Every value of the metric column is NULL, so no trend exists.",
            undefined="metric_all_null",
        )
    if metric_kind in {"ratio", "distinct"} and any(value < 0 for value in defined):
        return as_table(
            f"negative_values_not_applicable_{metric_kind}",
            f"A {metric_kind} metric cannot be negative, so the rows are not a "
            "valid chart input; they are shown as a table instead.",
            undefined="negative_values_not_applicable",
        )
    if chart_type == "table":
        return as_table(
            "caller_requested_table",
            "The caller explicitly requested a table.",
        )

    dimension_indexes = [index for index in range(len(column_names)) if index != metric_index]
    time_like = bool(grain and grain in TIME_GRAINS)
    point_count = len(defined)

    if point_count < 2 or len(row_values) < 2:
        parameters["point_count"] = point_count
        if metric_kind == "additive" and chart_type is None:
            return ChartSpec(
                chart_type="metric",
                reason="single_scalar_metric",
                rationale=(
                    "A single value is a scalar metric: a one-point line or bar "
                    "would fabricate a trend, so it is reported as a metric card."
                ),
                grain=grain,
                metric_kind=metric_kind,
                fields=fields,
                encoding={},
                table=table,
                row_count=len(row_values),
                point_count=point_count,
                parameters=parameters,
                undefined_reason="single_scalar_metric",
                limitations=limitations
                + ["A scalar has no shape to plot; only its value and window are meaningful."],
            )
        return as_table(
            "insufficient_points_for_chart",
            "Fewer than two comparable points exist, so no chart is justified.",
            undefined="insufficient_points_for_trend",
        )

    if len(dimension_indexes) > 2:
        return as_table(
            "too_many_dimensions",
            "More than two dimensions are supplied; they do not fit one chart "
            "encoding and require faceting that is not declared here.",
            undefined="too_many_dimensions",
        )

    if chart_type == "line" and not time_like:
        return as_table(
            "requested_line_not_justified",
            "A line chart was requested but the rows are not a time series "
            "(no declared time grain), so a line would imply an ordering.",
            undefined="line_requires_time_grain",
        )
    if chart_type == "metric":
        return as_table(
            "requested_metric_not_justified",
            "A single-value card was requested but the result holds multiple points.",
            undefined="metric_requires_single_value",
        )

    x_index = dimension_indexes[0] if dimension_indexes else None
    if x_index is None:
        return as_table(
            "no_dimension_for_x_axis",
            "Only the metric column is present, so there is no axis to plot against.",
            undefined="no_dimension_for_x_axis",
        )
    color_index = dimension_indexes[1] if len(dimension_indexes) == 2 and time_like else None
    encoding = {
        "x": fields[column_names[x_index]],
        "y": fields[column_names[metric_index]],
    }
    if color_index is not None:
        encoding["color"] = fields[column_names[color_index]]

    use_line = time_like and chart_type != "bar"
    chart = "line" if use_line else "bar"
    if chart == "line":
        reason = "time_series_with_at_least_two_points"
        rationale = (
            f"A {grain} time series with {point_count} observed points is plotted "
            "as a line (x = time bucket, y = metric)."
        )
    else:
        reason = "categorical_breakdown"
        rationale = (
            f"A categorical breakdown with {len(row_values)} rows is plotted as a "
            "bar chart; bars are never stacked because ratio and distinct metrics "
            "are not additive."
        )
    spec: dict[str, Any] = {
        "$schema": "https://vega-lite.github.io/schema/vega-lite/v5.json",
        "description": rationale,
        "data": {"values": _chart_rows(column_names, row_values, fields, x_index, metric_index, color_index)},
        "mark": {"type": chart, **({"point": True} if chart == "line" else {})},
        "encoding": {
            "x": {
                "field": encoding["x"],
                "type": "temporal" if time_like else "nominal",
                "title": column_names[x_index],
            },
            "y": {
                "field": encoding["y"],
                "type": "quantitative",
                "title": column_names[metric_index],
            },
        },
        "usermeta": {"method": "declared_chart_selection", "reason": reason, "metric_kind": metric_kind},
    }
    if color_index is not None:
        spec["encoding"]["color"] = {
            "field": encoding["color"],
            "type": "nominal",
            "title": column_names[color_index],
        }
    if metric_kind == "ratio":
        limitations.append(
            "A ratio over a dimension is not additive: the y axis must not be "
            "summed or stacked across categories."
        )
    if metric_kind == "distinct":
        limitations.append(
            "Distinct counts of overlapping groups cannot be summed; the bars are "
            "per-group counts only."
        )
    return ChartSpec(
        chart_type=chart,  # type: ignore[arg-type]
        reason=reason,
        rationale=rationale,
        grain=grain,
        metric_kind=metric_kind,
        fields=fields,
        encoding=encoding,
        vega_lite_spec=spec,
        table=table,
        row_count=len(row_values),
        point_count=point_count,
        parameters=parameters,
        limitations=limitations,
    )


# --------------------------------------------------------------------- helpers


def _nullable_number(value: Any, *, field_name: str) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{field_name} must be a number or None, got {value!r}")
    return float(value)


def _optional_float(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return float(str(value))
    except (TypeError, ValueError):
        return None


def _bucket_entry(
    bucket: Mapping[str, Any],
    *,
    index: int,
    keys: Sequence[str],
    tool: str,
) -> tuple[Any, ...]:
    """Return ``(category, *values)`` for one bucket, validating the shape."""


    if not isinstance(bucket, Mapping):
        raise ValueError(f"{tool}: bucket #{index} must be an object, got {type(bucket).__name__}")
    category = bucket.get("category")
    if category is None or not str(category).strip():
        raise ValueError(f"{tool}: bucket #{index} needs a non-empty 'category'")
    values: list[float | None] = []
    for key in keys:
        value = _nullable_number(bucket.get(key), field_name=f"bucket #{index}.{key}")
        if value is not None and not math.isfinite(value):
            raise ValueError(
                f"{tool}: bucket #{index}.{key} must be finite, got {value!r}; "
                "non-finite values cannot be ranked or summed"
            )
        values.append(value)
    return (str(category), *values)


def _series_entry(item: Mapping[str, Any], *, index: int) -> tuple[str, float | None]:
    if not isinstance(item, Mapping):
        raise ValueError(f"detect_anomaly: point #{index} must be an object, got {type(item).__name__}")
    period = item.get("period")
    if period is None or not str(period).strip():
        raise ValueError(f"detect_anomaly: point #{index} needs a non-empty 'period'")
    value = item.get("value")
    if value is None:
        return str(period), None
    number = _nullable_number(value, field_name=f"point #{index}.value")
    return str(period), number


def _median(values: Sequence[float]) -> float:
    ordered = sorted(values)
    length = len(ordered)
    if length == 0:
        raise ValueError("median of an empty sequence is undefined")
    middle = length // 2
    if length % 2 == 1:
        return ordered[middle]
    return (ordered[middle - 1] + ordered[middle]) / 2.0


def _parse_iso_date(value: str) -> _date | None:
    if not isinstance(value, str):
        return None
    try:
        return _date.fromisoformat(value.strip())
    except ValueError:
        return None


def _season_key(day: _date, seasonality: str) -> str:
    if seasonality == "weekly":
        return f"weekday_{day.weekday()}"
    return f"month_{day.month:02d}"


def _metric_column_index(columns: Sequence[str], rows: Sequence[Sequence[Any]]) -> int | None:
    """Last column whose values are numeric-or-null (the usual metric position).

    A column that is entirely NULL stays a candidate (vacuously numeric) so the
    caller gets the specific ``metric_all_null`` reason instead of the vaguer
    "no numeric column".
    """

    candidate: int | None = None
    for index in range(len(columns)):
        values = [row[index] if len(row) > index else None for row in rows]
        defined = [value for value in values if value is not None and not isinstance(value, bool)]
        if all(_optional_float(value) is not None for value in defined):
            candidate = index
    return candidate


def _safe_field_names(columns: Sequence[str]) -> dict[str, str]:
    used: dict[str, int] = {}
    mapping: dict[str, str] = {}
    for index, column in enumerate(columns):
        cleaned = "".join(char if char.isalnum() else "_" for char in str(column).strip().casefold())
        cleaned = "_".join(part for part in cleaned.split("_") if part) or f"field_{index}"
        if cleaned[0].isdigit():
            cleaned = f"f_{cleaned}"
        count = used.get(cleaned, 0)
        used[cleaned] = count + 1
        mapping[str(column)] = cleaned if count == 0 else f"{cleaned}_{count + 1}"
    return mapping


def _chart_rows(
    columns: Sequence[str],
    rows: Sequence[Sequence[Any]],
    fields: Mapping[str, str],
    x_index: int,
    metric_index: int,
    color_index: int | None,
) -> list[dict[str, Any]]:
    payload: list[dict[str, Any]] = []
    for row in rows:
        value = _optional_float(row[metric_index]) if len(row) > metric_index else None
        if value is None or not math.isfinite(value):
            # A missing point is omitted rather than plotted as 0.
            continue
        entry: dict[str, Any] = {
            fields[columns[x_index]]: row[x_index] if len(row) > x_index else None,
            fields[columns[metric_index]]: value,
        }
        if color_index is not None:
            entry[fields[columns[color_index]]] = row[color_index] if len(row) > color_index else None
        payload.append(entry)
    return payload


__all__ = [
    "ANOMALY_METHODS",
    "CHART_TYPES",
    "COMPARISON_METHODS",
    "FLOAT_TOLERANCE",
    "METRIC_KINDS",
    "MISSING_POLICIES",
    "SEASONALITY_KINDS",
    "TIME_GRAINS",
    "AnomalyPoint",
    "AnomalyReport",
    "ChartSpec",
    "ContributionBreakdown",
    "ContributionItem",
    "DrillDown",
    "DrillDownBucket",
    "PeriodComparison",
    "RatioBucket",
    "RatioCombination",
    "build_chart",
    "combine_ratio",
    "compare_periods",
    "contribution_breakdown",
    "detect_anomaly",
    "drill_down",
    "numbers_close",
    "require_consistent_grain",
    "require_consistent_units",
    "require_consistent_versions",
]
