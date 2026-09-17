"""Step-11 tests: deterministic analysis tools, inputs, and registry wiring.

Every numeric gold below is hand-computed in the comment above it; the module is
offline and deterministic (no LLM, no clock, no network), and all float
comparisons use the declared :data:`FLOAT_TOLERANCE` instead of ``==``.
"""

from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from queryforge.domain.analysis.analysis_tools import (
    FLOAT_TOLERANCE,
    build_chart,
    combine_ratio,
    compare_periods,
    contribution_breakdown,
    detect_anomaly,
    drill_down,
    numbers_close,
    require_consistent_grain,
    require_consistent_units,
    require_consistent_versions,
)
from queryforge.infrastructure.db.sqlite_connector import SQLiteConnector
from queryforge.infrastructure.tools.analysis_tool import (
    AnalysisInputAssembler,
    AnalysisInputBudget,
    MetricResolution,
    assert_usable,
)
from queryforge.infrastructure.tools.database_tool import DatabaseTool
from queryforge.orchestration.planner.plan import (
    AnalysisPlan,
    PlanStep,
    PlanValidator,
    PlanViolation,
)
from queryforge.orchestration.tools import (
    BudgetManager,
    ToolContext,
    ToolUnavailable,
    build_default_registry,
)

ANALYSIS_TOOLS = (
    "compare_periods",
    "drill_down",
    "calculate_contribution",
    "detect_anomaly",
    "render_chart",
)

#: The keys the analysis executor passes explicitly when it dispatches a step
#: (``planner/executor.py``: ``_assemble_step11``).  Each one must be declared in
#: the tool's parameter schema, otherwise ``additionalProperties: False`` rejects
#: the assembled call with ``extra_forbidden``.
EXECUTOR_KEYS: dict[str, tuple[str, ...]] = {
    "compare_periods": ("current", "baseline", "label", "method"),
    "drill_down": ("buckets", "total", "max_categories", "min_sample", "dimension"),
    "calculate_contribution": (
        "buckets",
        "expected_total_delta",
        "tolerance",
        "additive",
        "metric_kind",
    ),
    "detect_anomaly": (
        "series",
        "method",
        "min_points",
        "seasonality",
        "missing",
        "threshold",
    ),
    "render_chart": ("rows", "columns", "metric_kind", "grain", "chart_type"),
}

SHOCK_SERIES = [
    {"period": f"2025-01-{day:02d}", "value": value}
    for day, value in enumerate([10, 11, 9, 10, 11, 9, 10, 11, 9, 30], start=1)
]

ORDERS_SCHEMA = (
    "CREATE TABLE orders ("
    "order_id INTEGER PRIMARY KEY, channel TEXT, amount REAL, converted INTEGER, "
    "order_date TEXT, currency TEXT)"
)

def _fixture_rows() -> list[tuple]:
    """Hand-countable rows: web 9/10 converted (amount 10..100, 2025-01-*),
    app 100/1000 converted (amount 2.0, 2025-02-*, one row per day cycling)."""

    rows = [
        (index, "web", 10.0 * index, 0 if index == 3 else 1,
         f"2025-01-{index:02d}", "CNY")
        for index in range(1, 11)
    ]
    rows.extend(
        (1000 + offset, "app", 2.0, 1 if offset < 100 else 0,
         f"2025-02-{(offset % 28) + 1:02d}", "CNY")
        for offset in range(1_000)
    )
    return rows


class OrdersFixture(unittest.TestCase):
    """Shared governed SQLite fixture (orders table, default policy)."""

    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        root = Path(self.directory.name)
        self.database = root / "orders.sqlite"
        connection = sqlite3.connect(self.database)
        connection.execute(ORDERS_SCHEMA)
        connection.executemany("INSERT INTO orders VALUES (?, ?, ?, ?, ?, ?)", _fixture_rows())
        connection.commit()
        connection.close()

    def tearDown(self) -> None:
        self.directory.cleanup()

    def database_tool(self) -> DatabaseTool:
        connector = SQLiteConnector(str(self.database))
        self.addCleanup(connector.close)
        return DatabaseTool(connector)

    def assembler(self, *, max_rows: int = 1_000) -> AnalysisInputAssembler:
        return AnalysisInputAssembler(
            self.database_tool(), AnalysisInputBudget(max_rows=max_rows)
        )


def revenue_metric(**overrides) -> MetricResolution:
    payload = {
        "name": "revenue",
        "entity_table": "orders",
        "aggregation": "sum",
        "expression": "amount",
        "time_field": "order_date",
        "unit": "CNY",
        "version": "v1",
    }
    payload.update(overrides)
    return MetricResolution(**payload)


class PeriodComparisonTest(unittest.TestCase):
    """11-N1 / 11-B1: absolute and relative change with explicit states."""

    def test_gold_80_vs_100(self) -> None:
        # (80 - 100) = -20; -20 / |100| = -0.2; -0.2 * 100 = -20%.
        result = compare_periods(80, 100)
        self.assertEqual(result.state, "ok")
        self.assertEqual(result.delta, -20.0)
        self.assertEqual(result.relative_change, -0.2)
        self.assertEqual(result.percent_change, -20.0)
        self.assertIsNone(result.undefined_reason)
        self.assertEqual(result.method, "absolute_relative")
        self.assertEqual(result.parameters["relative_denominator"], "abs(baseline)")
        self.assertTrue(any("not a trend estimate" in item for item in result.limitations))

    def test_boundary_states_never_fabricate_a_percentage(self) -> None:
        both_zero = compare_periods(0, 0)
        self.assertEqual((both_zero.state, both_zero.delta), ("both_zero", 0.0))
        self.assertIsNone(both_zero.relative_change)
        self.assertIsNone(both_zero.percent_change)
        self.assertEqual(both_zero.undefined_reason, "both_zero")

        zero_baseline = compare_periods(7, 0)
        self.assertEqual((zero_baseline.state, zero_baseline.delta), ("zero_baseline", 7.0))
        self.assertIsNone(zero_baseline.relative_change)
        self.assertIsNone(zero_baseline.percent_change)
        self.assertEqual(zero_baseline.undefined_reason, "zero_baseline")

        # A negative baseline keeps the sign of the absolute change.
        # (-80 - -100) = +20; 20 / |-100| = +0.2.
        negative = compare_periods(-80, -100)
        self.assertEqual(negative.state, "ok")
        self.assertEqual(negative.delta, 20.0)
        self.assertEqual(negative.relative_change, 0.2)

        missing = compare_periods(80, None)
        self.assertEqual(missing.state, "missing_value")
        self.assertIsNone(missing.delta)
        self.assertIsNone(missing.percent_change)
        self.assertEqual(missing.parameters["missing"], ["baseline"])

        infinite = compare_periods(float("inf"), 1)
        self.assertEqual(infinite.state, "undefined_input")
        self.assertEqual(infinite.undefined_reason, "non_finite_input")
        self.assertIsNone(infinite.percent_change)

        nan = compare_periods(float("nan"), 1)
        self.assertEqual(nan.state, "undefined_input")
        self.assertIsNone(nan.relative_change)

    def test_declared_method_limits_are_respected(self) -> None:
        absolute_only = compare_periods(80, 100, method="absolute_only")
        self.assertEqual(absolute_only.delta, -20.0)
        self.assertIsNone(absolute_only.relative_change)
        self.assertEqual(absolute_only.state, "method_limited")
        with self.assertRaises(ValueError):
            compare_periods(1, 2, method="compounded_growth")


class DrillDownTest(unittest.TestCase):
    """11-P1: a high-cardinality breakdown stays bounded and honest."""

    def test_high_cardinality_is_bounded_with_others_and_coverage(self) -> None:
        buckets = [
            {"category": f"c{index:04d}", "value": float(1_000 - index)}
            for index in range(1_000)
        ]
        # Bucket i has value 1000-i, so the total is sum(1..1000) = 500500.
        total = 500_500.0
        result = drill_down(buckets, total=total, max_categories=5)
        self.assertEqual(result.kept_count, 5)
        self.assertEqual(
            [bucket.value for bucket in result.buckets], [1000.0, 999.0, 998.0, 997.0, 996.0]
        )
        self.assertEqual(result.others_category_count, 995)
        self.assertIsNotNone(result.others)
        # others = 500500 - (996..1000) = 500500 - 4990 = 495510.
        self.assertEqual(result.others.value, 495_510.0)
        self.assertTrue(result.truncated)
        # coverage = 4990 / 500500.
        self.assertAlmostEqual(result.coverage, 4_990 / 500_500, places=12)
        self.assertEqual(result.coverage_basis, "declared_total")
        self.assertEqual(
            result.others_reasons["tail"], [f"c{index:04d}" for index in range(5, 1_000)]
        )
        shares = sum(bucket.share for bucket in result.buckets) + result.others.share
        self.assertTrue(numbers_close(shares, 1.0, tolerance=1e-9))

    def test_below_min_sample_is_grouped_with_a_reason(self) -> None:
        result = drill_down(
            [
                {"category": "big", "value": 100.0},
                {"category": "tiny_a", "value": 1.0},
                {"category": "tiny_b", "value": 2.0},
            ],
            max_categories=2,
            min_sample=5.0,
        )
        self.assertEqual([bucket.category for bucket in result.buckets], ["big"])
        # Grouped in rank order (value desc), which is the order a reader sees.
        self.assertEqual(result.others_reasons["below_min_sample"], ["tiny_b", "tiny_a"])
        self.assertEqual(result.others.value, 3.0)
        self.assertTrue(result.truncated)

    def test_max_categories_must_be_positive(self) -> None:
        with self.assertRaises(ValueError):
            drill_down([{"category": "a", "value": 1.0}], max_categories=0)
        with self.assertRaises(ValueError):
            drill_down([{"category": "a", "value": float("inf")}])

    def test_empty_input_is_a_declared_state(self) -> None:
        result = drill_down([])
        self.assertEqual(result.undefined_reason, "empty_buckets")
        self.assertIsNone(result.coverage)
        self.assertEqual(result.coverage_basis, "none")


class ContributionTest(unittest.TestCase):
    """11-N2 / 11-B3: additive decomposition, refusal of non-additive inputs."""

    def test_gold_a_60_to_30_and_b_40_to_50(self) -> None:
        # A: 30-60 = -30, B: 50-40 = +10; total -20; residual 0.
        result = contribution_breakdown(
            [
                {"category": "A", "current": 30, "baseline": 60},
                {"category": "B", "current": 50, "baseline": 40},
            ],
            expected_total_delta=-20,
        )
        self.assertEqual(result.total_delta, -20.0)
        self.assertEqual(result.computed_total_delta, -20.0)
        self.assertEqual(result.residual, 0.0)
        self.assertTrue(result.residual_explained)
        by_category = {item.category: item for item in result.contributions}
        self.assertEqual(by_category["A"].delta, -30.0)
        self.assertEqual(by_category["B"].delta, 10.0)
        # Shares are delta_i / total_delta: A = -30 / -20 = 1.5, B = 10 / -20 = -0.5.
        self.assertEqual(by_category["A"].share, 1.5)
        self.assertEqual(by_category["B"].share, -0.5)
        self.assertEqual(by_category["A"].direction, "decrease")
        self.assertTrue(by_category["B"].offsetting)
        self.assertTrue(any("not a causal attribution" in item for item in result.limitations))

    def test_unreconciled_total_is_reported_not_hidden(self) -> None:
        result = contribution_breakdown(
            [
                {"category": "A", "current": 30, "baseline": 60},
                {"category": "B", "current": 50, "baseline": 40},
            ],
            expected_total_delta=-15,
        )
        self.assertEqual(result.residual, -5.0)
        self.assertFalse(result.residual_explained)
        self.assertTrue(any("do not reconcile" in item for item in result.limitations))

    def test_ratio_and_distinct_refuse_summation(self) -> None:
        buckets = [{"category": "A", "current": 30, "baseline": 60}]
        ratio = contribution_breakdown(buckets, metric_kind="ratio")
        self.assertEqual(ratio.undefined_reason, "ratio_not_additive")
        self.assertEqual(ratio.contributions, [])
        self.assertIsNone(ratio.total_delta)
        self.assertTrue(any("numerator" in item for item in ratio.limitations))

        distinct = contribution_breakdown(buckets, metric_kind="distinct")
        self.assertEqual(distinct.undefined_reason, "distinct_not_additive")
        self.assertEqual(distinct.contributions, [])

        overlapping = contribution_breakdown(buckets, additive=False)
        self.assertEqual(overlapping.undefined_reason, "non_additive_input")
        self.assertEqual(overlapping.contributions, [])

    def test_zero_total_change_leaves_shares_undefined(self) -> None:
        result = contribution_breakdown(
            [
                {"category": "A", "current": 30, "baseline": 40},
                {"category": "B", "current": 50, "baseline": 40},
            ]
        )
        self.assertEqual(result.total_delta, 0.0)
        self.assertEqual(result.share_undefined_reason, "zero_total_delta")
        self.assertTrue(all(item.share is None for item in result.contributions))
        # Ties on |delta| are broken by category, so A (-10) precedes B (+10).
        self.assertEqual([item.delta for item in result.contributions], [-10.0, 10.0])


class RatioCombinationTest(OrdersFixture):
    """11-B2: ratios pool from real numerator/denominator, never as a mean."""

    def test_weighted_ratio_differs_from_an_unweighted_mean(self) -> None:
        # web: 9/10 = 0.9, app: 100/1000 = 0.1.
        # pooled = 109 / 1010 = 0.1079207920792079..., unweighted mean = 0.5.
        numerator = self.assembler().metric_by_dimension(
            revenue_metric(
                name="converted_orders",
                aggregation="sum",
                expression="converted",
                time_field=None,
            ),
            dimension="channel",
        )
        denominator = self.assembler().metric_by_dimension(
            revenue_metric(
                name="order_count",
                aggregation="count",
                expression=None,
                time_field=None,
            ),
            dimension="channel",
        )
        buckets = [
            {
                "category": bucket["category"],
                "numerator": bucket["value"],
                "denominator": counts[bucket["category"]],
            }
            for bucket in numerator.buckets
            for counts in [{item["category"]: item["value"] for item in denominator.buckets}]
        ]
        self.assertEqual(
            sorted((bucket["category"], bucket["numerator"], bucket["denominator"]) for bucket in buckets),
            [("app", 100.0, 1000.0), ("web", 9.0, 10.0)],
        )

        result = combine_ratio(buckets)
        self.assertAlmostEqual(result.ratio, 109 / 1010, places=12)
        self.assertEqual(result.unweighted_mean_ratio, 0.5)
        self.assertNotAlmostEqual(result.ratio, result.unweighted_mean_ratio, places=6)
        self.assertEqual(result.total_numerator, 109.0)
        self.assertEqual(result.total_denominator, 1010.0)
        self.assertTrue(
            any("must not be quoted" in item for item in result.limitations)
        )
        with self.assertRaises(ValueError):
            combine_ratio(buckets, method="mean_of_ratios")

    def test_zero_denominator_is_undefined(self) -> None:
        result = combine_ratio([{"category": "a", "numerator": 1.0, "denominator": 0.0}])
        self.assertIsNone(result.ratio)
        self.assertEqual(result.undefined_reason, "zero_denominator")


class AnomalyDetectionTest(unittest.TestCase):
    """11-B4: declared baseline method, missing policy, no fabricated anomalies."""

    SHOCK = [
        (f"2025-01-{day:02d}", value)
        for day, value in enumerate([10, 11, 9, 10, 11, 9, 10, 11, 9, 30], start=1)
    ]

    def _series(self, rows, **overrides):
        return detect_anomaly([{"period": period, "value": value} for period, value in rows], **overrides)

    def test_few_points_are_insufficient_and_constant_series_has_no_anomaly(self) -> None:
        short = self._series([("2025-01-01", 1.0), ("2025-01-02", 2.0)])
        self.assertEqual(short.state, "insufficient_data")
        self.assertEqual(short.undefined_reason, "insufficient_data")
        self.assertEqual(short.anomalies, [])
        self.assertEqual(short.observed_n, 2)

        constant = self._series(
            [(f"2025-01-{day:02d}", 10.0) for day in range(1, 9)]
        )
        self.assertEqual(constant.state, "constant_series")
        self.assertEqual(constant.anomalies, [])
        self.assertEqual({point.score for point in constant.points}, {0.0})
        self.assertEqual(constant.baseline_scale, 0.0)

    def test_planted_shock_is_detected_by_the_declared_method(self) -> None:
        report = self._series(self.SHOCK)
        # median = 10, MAD = 1 -> scale = 1.4826; 30 - 10 = 20 -> score 13.49.
        self.assertEqual(report.state, "ok")
        self.assertEqual(report.baseline_center, 10.0)
        self.assertAlmostEqual(report.baseline_scale, 1.4826, places=12)
        self.assertEqual(report.scale_method, "median_absolute_deviation_x1.4826")
        self.assertEqual([point.period for point in report.anomalies], ["2025-01-10"])
        self.assertAlmostEqual(report.anomalies[0].score, 20 / 1.4826, places=9)
        self.assertEqual(report.anomalies[0].direction, "above")
        self.assertIn("median", report.method_description)
        self.assertTrue(any("no causal claim" in item for item in report.limitations))
        self.assertTrue(any("not a significance test" in item for item in report.limitations))

    def test_zscore_method_is_declared_and_differs(self) -> None:
        report = self._series(self.SHOCK, method="zscore")
        self.assertIn("population standard deviation", report.method_description)
        self.assertEqual(report.scale_method, "population_standard_deviation")
        self.assertEqual([point.period for point in report.anomalies], ["2025-01-10"])

    def test_missing_day_policies_are_explicit(self) -> None:
        rows = [
            ("2025-01-01", 10.0),
            ("2025-01-02", 10.0),
            ("2025-01-03", None),
            ("2025-01-04", 10.0),
            ("2025-01-05", 10.0),
            ("2025-01-06", 10.0),
            ("2025-01-07", 10.0),
            ("2025-01-08", 10.0),
            ("2025-01-09", 10.0),
        ]
        skipped = self._series(rows, missing="skip")
        gap_point = next(point for point in skipped.points if point.period == "2025-01-03")
        self.assertIsNone(gap_point.score)
        self.assertEqual(gap_point.reason, "missing_value")
        self.assertFalse(gap_point.is_anomaly)
        self.assertEqual(skipped.baseline_n, 8)

        filled = self._series(rows, missing="gap")
        filled_point = next(point for point in filled.points if point.period == "2025-01-03")
        self.assertTrue(filled_point.gap_filled)
        self.assertEqual(filled_point.reason, "missing_value_gap_filled")
        self.assertFalse(filled_point.is_anomaly)
        self.assertEqual(filled.baseline_n, 9)

    def test_weekly_seasonality_removes_a_repeating_weekday_pattern(self) -> None:
        # 2025-01-06 is a Monday: two Mondays at 100, every other day at 10.
        rows = [
            (
                (__import__("datetime").date(2025, 1, 6) + __import__("datetime").timedelta(days=offset)).isoformat(),
                100.0 if offset in (0, 7) else 10.0,
            )
            for offset in range(14)
        ]
        raw = self._series(rows)
        self.assertEqual(len(raw.anomalies), 2)

        adjusted = self._series(rows, seasonality="weekly")
        self.assertTrue(adjusted.seasonality_applied)
        self.assertEqual(adjusted.seasonal_medians["weekday_0"], 100.0)
        self.assertEqual(adjusted.anomalies, [])

        coarse = self._series([("2025-01", 10.0)] * 6, seasonality="monthly")
        self.assertEqual(coarse.state, "undefined_input")
        self.assertEqual(coarse.undefined_reason, "seasonality_requires_iso_dates")

    def test_invalid_declared_options_are_refused(self) -> None:
        with self.assertRaises(ValueError):
            self._series(self.SHOCK, method="prophet")
        with self.assertRaises(ValueError):
            self._series(self.SHOCK, min_points=1)
        with self.assertRaises(ValueError):
            self._series(self.SHOCK, missing="interpolate")


class ChartSpecTest(unittest.TestCase):
    """Charts are chosen from metric kind + grain, with honest fallbacks."""

    def test_time_grain_and_categorical_shapes(self) -> None:
        line = build_chart(
            [["2025-01-01", 10.0], ["2025-01-02", 12.0]],
            ["order_date", "revenue"],
            grain="daily",
        )
        self.assertEqual(line.chart_type, "line")
        self.assertEqual(line.reason, "time_series_with_at_least_two_points")
        self.assertEqual(line.fields["revenue"], "revenue")
        self.assertEqual(line.vega_lite_spec["mark"]["type"], "line")
        self.assertEqual(line.vega_lite_spec["encoding"]["x"]["field"], "order_date")

        bar = build_chart([["web", 30.0], ["app", 70.0]], ["channel", "revenue"])
        self.assertEqual(bar.chart_type, "bar")
        self.assertEqual(bar.reason, "categorical_breakdown")
        self.assertEqual(bar.vega_lite_spec["mark"]["type"], "bar")

    def test_unjustified_charts_fall_back_to_a_table(self) -> None:
        empty = build_chart([], ["channel", "revenue"])
        self.assertEqual(empty.chart_type, "table")
        self.assertEqual(empty.reason, "empty_result")
        self.assertIsNone(empty.vega_lite_spec)

        all_null = build_chart([["web", None], ["app", None]], ["channel", "revenue"])
        self.assertEqual((all_null.chart_type, all_null.undefined_reason), ("table", "metric_all_null"))

        single = build_chart([["web", 30.0]], ["channel", "revenue"])
        self.assertEqual(single.chart_type, "metric")
        self.assertEqual(single.reason, "single_scalar_metric")
        self.assertIsNone(single.vega_lite_spec)

        no_metric = build_chart([["web", "x"], ["app", "y"]], ["channel", "note"])
        self.assertEqual(no_metric.undefined_reason, "no_numeric_metric")

    def test_ratio_negatives_and_requested_line_are_refused(self) -> None:
        negative_ratio = build_chart(
            [["web", -0.1], ["app", -0.2]], ["channel", "conversion"], metric_kind="ratio"
        )
        self.assertEqual(negative_ratio.chart_type, "table")
        self.assertEqual(negative_ratio.undefined_reason, "negative_values_not_applicable")

        requested = build_chart(
            [["web", 1.0], ["app", 2.0]], ["channel", "revenue"], chart_type="line"
        )
        self.assertEqual(requested.chart_type, "table")
        self.assertEqual(requested.undefined_reason, "line_requires_time_grain")

        with self.assertRaises(ValueError):
            build_chart([["a", 1.0]], ["c", "v"], chart_type="pie")

    def test_field_names_are_sanitized_and_stable(self) -> None:
        spec = build_chart(
            [["web", 1.0], ["app", 2.0]], ["Total Revenue (USD)", "订单数"]
        )
        self.assertEqual(spec.fields["Total Revenue (USD)"], "total_revenue_usd")
        self.assertTrue(set(spec.fields.values()) == {spec.fields["订单数"], "total_revenue_usd"})
        self.assertEqual(
            spec.vega_lite_spec["encoding"]["y"]["field"], spec.fields["订单数"]
        )


class InputGuardTest(unittest.TestCase):
    """11-E1: mixed units / versions / grains are refused, never merged."""

    def test_mixed_units_and_versions_raise_a_named_reason(self) -> None:
        self.assertEqual(require_consistent_units({"CNY", None, "CNY"}), "CNY")
        with self.assertRaises(ValueError) as caught:
            require_consistent_units({"CNY", "USD"})
        self.assertIn("mixed_units", str(caught.exception))
        self.assertIn("USD", str(caught.exception))

        with self.assertRaises(ValueError) as versions:
            require_consistent_versions({"v1", "v2"})
        self.assertIn("mixed_versions", str(versions.exception))

        with self.assertRaises(ValueError):
            require_consistent_grain({"daily", "monthly"})
        self.assertIsNone(require_consistent_units({None}))


class DeterminismTest(unittest.TestCase):
    """11-R1: row order / chunking do not change additive results."""

    def test_row_order_does_not_change_additive_results(self) -> None:
        buckets = [
            {"category": "web", "value": 30.0},
            {"category": "app", "value": 70.0},
            {"category": "store", "value": 5.0},
        ]
        forward = drill_down(buckets, total=105.0, max_categories=2)
        reverse = drill_down(list(reversed(buckets)), total=105.0, max_categories=2)
        self.assertEqual(forward.model_dump(), reverse.model_dump())

        changes = [
            {"category": "A", "current": 30, "baseline": 60},
            {"category": "B", "current": 50, "baseline": 40},
            {"category": "C", "current": 12, "baseline": 2},
        ]
        forward_contribution = contribution_breakdown(changes, expected_total_delta=-10)
        reverse_contribution = contribution_breakdown(
            list(reversed(changes)), expected_total_delta=-10
        )
        self.assertEqual(forward_contribution.model_dump(), reverse_contribution.model_dump())

    def test_chunked_input_sums_to_the_whole_within_the_declared_tolerance(self) -> None:
        buckets = [
            {"category": f"c{index}", "current": float(index), "baseline": float(index) / 2}
            for index in range(1, 21)
        ]
        whole = contribution_breakdown(buckets)
        parts = [
            contribution_breakdown(buckets[:7]).computed_total_delta,
            contribution_breakdown(buckets[7:]).computed_total_delta,
        ]
        self.assertTrue(
            numbers_close(sum(parts), whole.computed_total_delta, tolerance=FLOAT_TOLERANCE)
        )
        self.assertFalse(numbers_close(1.0, 1.0 + 1e-6, tolerance=FLOAT_TOLERANCE))

    def test_series_scan_does_not_depend_on_row_order(self) -> None:
        rows = [
            {"period": f"2025-01-{day:02d}", "value": value}
            for day, value in enumerate([10, 11, 9, 10, 11, 9, 10, 11, 9, 30], start=1)
        ]
        forward = detect_anomaly(rows)
        shuffled = detect_anomaly(list(reversed(rows)))
        self.assertEqual(
            {point.period: point.score for point in forward.points},
            {point.period: point.score for point in shuffled.points},
        )
        self.assertEqual(
            [point.period for point in forward.anomalies],
            [point.period for point in shuffled.anomalies],
        )


class AnalysisToolRegistryTest(unittest.TestCase):
    """The five tools are registered, validated, and usable without a database."""

    def setUp(self) -> None:
        # No governed database is bound on purpose: none of the five tools may
        # need one (a chart must not require data access).
        self.registry = build_default_registry(None, BudgetManager())
        self.context = ToolContext(run_id="run-11", data_version="v1")

    def test_the_five_analysis_tools_are_registered_and_available(self) -> None:
        from queryforge.orchestration.tools import PLACEHOLDER_TOOLS

        self.assertEqual(PLACEHOLDER_TOOLS, ())
        for name in ANALYSIS_TOOLS:
            spec = self.registry.resolve(name)
            self.assertTrue(self.registry.is_available(name), name)
            self.assertEqual(spec.modes, ["execute"], name)
            self.assertTrue(spec.idempotent, name)
            self.assertEqual(spec.permissions, [], name)
            self.assertEqual(
                spec.budget_category,
                "render" if name == "render_chart" else "compute",
                name,
            )
            self.assertTrue(spec.parameter_schema["properties"], name)
            # Integration convention: every parameter is optional, so the planner
            # can validate a planning-stage step whose ``inputs`` are still {}.
            self.assertEqual(spec.parameter_schema.get("required", []), [], name)
            self.assertFalse(spec.parameter_schema["additionalProperties"], name)

    def test_unknown_parameters_are_rejected_before_execution(self) -> None:
        observation = self.registry.execute(
            "compare_periods", {"current": 80, "baseline": 100, "limit": 5}, context=self.context
        )
        self.assertEqual(observation.status, "denied")
        self.assertEqual(observation.error_category, "unknown")
        self.assertIn("invalid_tool_params", observation.call.error)
        self.assertIsNone(observation.result)

        bad_enum = self.registry.execute(
            "detect_anomaly",
            {"series": [{"period": "2025-01-01", "value": 1}], "seasonality": "hourly"},
            context=self.context,
        )
        self.assertEqual(bad_enum.status, "denied")

    def test_planning_stage_inputs_validate_even_though_they_are_empty(self) -> None:
        """The planner validates ``step.inputs`` (still {}) against this schema."""

        plan = AnalysisPlan(
            question="q",
            steps=[PlanStep(id=name, action=name) for name in ANALYSIS_TOOLS],
        )
        PlanValidator.validate(plan, self.registry, "execute")  # must not raise

        with self.assertRaises(PlanViolation) as unknown:
            PlanValidator.validate(
                AnalysisPlan(
                    question="q",
                    steps=[
                        PlanStep(
                            id="chart",
                            action="render_chart",
                            inputs={"rows": [], "columns": [], "typo": 1},
                        )
                    ],
                ),
                self.registry,
                "execute",
            )
        self.assertIn("invalid_params", str(unknown.exception))

        # These tools are execute-only, so a plan_only run refuses the step.
        with self.assertRaises(PlanViolation):
            PlanValidator.validate(plan, self.registry, "plan_only")

    def test_empty_call_is_refused_instead_of_inventing_numbers(self) -> None:
        self.assertTrue(issubclass(ToolUnavailable, ValueError))
        for name in ANALYSIS_TOOLS:
            observation = self.registry.execute(name, {}, context=self.context)
            self.assertEqual(observation.status, "failed", name)
            self.assertEqual(observation.error_category, "unknown", name)
            self.assertIn("missing_analysis_input", observation.call.error, name)
            self.assertIsNone(observation.result, name)

            # The handler itself raises a ValueError carrying the reason, instead
            # of relying on a schema-level ``required`` (which would reject every
            # planning-stage step).
            handler = self.registry.handler_for(name)
            with self.assertRaises(ToolUnavailable) as raised:
                handler(self.registry.validate_params(name, {}), self.context)
            self.assertEqual(raised.exception.reason, "missing_analysis_input")
            self.assertIn(name, str(raised.exception))

        # A caller that supplies its inputs still gets the declared data states.
        partial = self.registry.execute(
            "drill_down", {"buckets": []}, context=self.context
        )
        self.assertEqual(partial.status, "succeeded")
        self.assertEqual(partial.result["undefined_reason"], "empty_buckets")

    def test_every_executor_parameter_is_declared_and_accepted(self) -> None:
        """A call built only from the executor's keys must validate and run."""

        assembled = {
            "compare_periods": {
                # The executor sets current = newest point, baseline = previous
                # one, so a 60 -> 45 decline arrives as current=45/baseline=60.
                "current": 45,
                "baseline": 60,
                "label": "2025-01-01 -> 2025-01-02",
                "method": "absolute_relative",
            },
            "drill_down": {
                "buckets": [{"category": "a", "value": 60.0}, {"category": "b", "value": 40.0}],
                "total": 100.0,
                "max_categories": 10,
                "min_sample": None,
                "dimension": None,
            },
            "calculate_contribution": {
                "buckets": [{"category": "A", "current": 30, "baseline": 60}],
                "expected_total_delta": -30.0,
                "tolerance": 1e-9,
                "additive": True,
                "metric_kind": "additive",
            },
            "detect_anomaly": {
                "series": SHOCK_SERIES,
                "method": "baseline_deviation",
                "min_points": 4,
                "seasonality": "none",
                "missing": "skip",
                "threshold": 2.0,
            },
            "render_chart": {
                "rows": [["web", 30.0], ["app", 70.0]],
                "columns": ["channel", "revenue"],
                "metric_kind": "additive",
                "grain": None,
                "chart_type": None,
            },
        }
        for name, params in assembled.items():
            declared = set(self.registry.resolve(name).parameter_schema["properties"])
            self.assertTrue(
                set(EXECUTOR_KEYS[name]) <= declared,
                f"{name}: undeclared executor keys {sorted(set(EXECUTOR_KEYS[name]) - declared)}",
            )
            self.assertEqual(sorted(params), sorted(EXECUTOR_KEYS[name]), name)
            observation = self.registry.execute(name, params, context=self.context)
            self.assertEqual(observation.status, "succeeded", (name, observation.call.error))
        # Rehearsal gold: 60 -> 45 is delta -15, relative -0.25.
        comparison = self.registry.execute(
            "compare_periods", assembled["compare_periods"], context=self.context
        )
        self.assertEqual(comparison.result["delta"], -15.0)
        self.assertEqual(comparison.result["relative_change"], -0.25)

    def test_omitted_options_use_their_declared_defaults(self) -> None:
        """Validation must not forward ``None`` where the tool declares a default."""

        # The reported failure mode: detect_anomaly(method=None) used to raise
        # "unsupported anomaly method None".
        omitted = self.registry.execute(
            "detect_anomaly", {"series": SHOCK_SERIES}, context=self.context
        )
        self.assertEqual(omitted.status, "succeeded")
        self.assertEqual(omitted.result["method"], "baseline_deviation")
        self.assertEqual(omitted.result["scale_method"], "median_absolute_deviation_x1.4826")
        self.assertEqual(len(omitted.result["anomalies"]), 1)

        # An explicit null on a string knob means "unspecified" too.
        nulled = self.registry.execute(
            "detect_anomaly",
            {
                "series": SHOCK_SERIES,
                "method": None,
                "seasonality": None,
                "missing": None,
            },
            context=self.context,
        )
        self.assertEqual(nulled.status, "succeeded")
        self.assertEqual(nulled.result["method"], "baseline_deviation")
        self.assertEqual(nulled.result["seasonality"], "none")
        self.assertEqual(nulled.result["missing_policy"], "skip")

        # The declared defaults are visible in the schema (and in validate_params).
        for name, key, expected in (
            ("compare_periods", "method", "absolute_relative"),
            ("drill_down", "max_categories", 10),
            ("calculate_contribution", "tolerance", FLOAT_TOLERANCE),
            ("calculate_contribution", "additive", True),
            ("calculate_contribution", "metric_kind", "additive"),
            ("detect_anomaly", "method", "baseline_deviation"),
            ("detect_anomaly", "min_points", 6),
            ("detect_anomaly", "seasonality", "none"),
            ("detect_anomaly", "missing", "skip"),
            ("detect_anomaly", "threshold", 2.0),
            ("render_chart", "metric_kind", "additive"),
        ):
            schema = self.registry.resolve(name).parameter_schema["properties"][key]
            self.assertEqual(schema.get("default"), expected, f"{name}.{key}")

        # A caller that omits the options still gets the same analysis.
        drill = self.registry.execute(
            "drill_down",
            {"buckets": [{"category": f"c{i}", "value": float(20 - i)} for i in range(1, 13)]},
            context=self.context,
        )
        self.assertEqual(drill.result["kept_count"], 10)  # default max_categories
        self.assertEqual(drill.result["others_category_count"], 2)

        contribution = self.registry.execute(
            "calculate_contribution",
            {
                "buckets": [
                    {"category": "A", "current": 30, "baseline": 60},
                    {"category": "B", "current": 50, "baseline": 40},
                ],
                "expected_total_delta": -20,
            },
            context=self.context,
        )
        self.assertEqual(contribution.result["metric_kind"], "additive")
        self.assertEqual(contribution.result["tolerance"], FLOAT_TOLERANCE)
        self.assertTrue(contribution.result["residual_explained"])

        chart = self.registry.execute(
            "render_chart",
            {"rows": [["web", 30.0], ["app", 70.0]], "columns": ["channel", "revenue"]},
            context=self.context,
        )
        self.assertEqual(chart.result["chart_type"], "bar")
        self.assertEqual(chart.result["metric_kind"], "additive")

        # Numeric knobs must be omitted rather than nulled: a null number is not a
        # number, and a junk value must never be silently coerced into one.
        for name, params in (
            ("detect_anomaly", {"series": SHOCK_SERIES, "min_points": None}),
            ("detect_anomaly", {"series": SHOCK_SERIES, "threshold": None}),
            ("detect_anomaly", {"series": SHOCK_SERIES, "min_points": "many"}),
            ("drill_down", {"buckets": [], "max_categories": None}),
        ):
            observation = self.registry.execute(name, params, context=self.context)
            self.assertEqual(observation.status, "denied", (name, params))
            self.assertEqual(observation.error_category, "unknown", (name, params))
            self.assertIn("invalid_tool_params", observation.call.error)

    def test_real_calls_succeed_with_declared_methods(self) -> None:
        comparison = self.registry.execute(
            "compare_periods",
            {"current": 80, "baseline": 100, "unit": "CNY", "version": "v1"},
            context=self.context,
        )
        self.assertEqual(comparison.status, "succeeded")
        self.assertEqual(comparison.result["delta"], -20.0)
        self.assertEqual(comparison.result["version"], "v1")
        self.assertEqual(comparison.result["unit"], "CNY")

        # A tool-declared version that contradicts the run's data version is
        # refused instead of being silently merged (11-E1).
        mismatch = self.registry.execute(
            "compare_periods", {"current": 80, "baseline": 100, "version": "v9"}, context=self.context
        )
        self.assertEqual(mismatch.status, "failed")
        self.assertIn("mixed_versions", mismatch.call.error)

        drill = self.registry.execute(
            "drill_down",
            {
                "buckets": [
                    {"category": "a", "value": 60.0},
                    {"category": "b", "value": 40.0},
                    {"category": "c", "value": 5.0},
                ],
                "total": 105.0,
                "max_categories": 2,
            },
            context=self.context,
        )
        self.assertEqual(drill.status, "succeeded")
        self.assertEqual(drill.result["others"]["value"], 5.0)
        self.assertTrue(drill.result["truncated"])

        contribution = self.registry.execute(
            "calculate_contribution",
            {
                "buckets": [
                    {"category": "A", "current": 30, "baseline": 60},
                    {"category": "B", "current": 50, "baseline": 40},
                ],
                "expected_total_delta": -20,
            },
            context=self.context,
        )
        self.assertEqual(contribution.result["residual"], 0.0)
        self.assertTrue(contribution.result["residual_explained"])

        anomaly = self.registry.execute(
            "detect_anomaly",
            {
                "series": [
                    {"period": f"2025-01-{day:02d}", "value": value}
                    for day, value in enumerate([10, 11, 9, 10, 11, 9, 10, 11, 9, 30], start=1)
                ]
            },
            context=self.context,
        )
        self.assertEqual(anomaly.status, "succeeded")
        self.assertEqual(len(anomaly.result["anomalies"]), 1)

    def test_chart_tool_needs_no_database_and_falls_back_honestly(self) -> None:
        chart = self.registry.execute(
            "render_chart",
            {"rows": [["web", 30.0], ["app", 70.0]], "columns": ["channel", "revenue"]},
            context=self.context,
        )
        self.assertEqual(chart.status, "succeeded")
        self.assertEqual(chart.result["chart_type"], "bar")
        self.assertEqual(chart.result["evidence_kind"], "chart_spec")

        fallback = self.registry.execute(
            "render_chart", {"rows": [], "columns": ["channel", "revenue"]}, context=self.context
        )
        self.assertEqual(fallback.result["chart_type"], "table")
        self.assertEqual(fallback.result["undefined_reason"], "empty_result")
        self.assertIsNone(fallback.result["vega_lite_spec"])

    def test_sql_tools_still_require_a_governed_connection(self) -> None:
        missing = self.registry.execute("list_tables", {}, context=self.context)
        self.assertEqual(missing.status, "failed")
        self.assertIn("no governed database tool", missing.call.error)


class AnalysisInputAssemblerTest(OrdersFixture):
    """Inputs come from governed SQL with explicit metadata and truncation."""

    def test_metric_value_dimension_and_period_use_governed_sql(self) -> None:
        assembler = self.assembler()
        value = assembler.metric_value(revenue_metric(name="web_revenue"))
        self.assertEqual(value.method, "governed_sql")
        self.assertEqual(value.unit, "CNY")
        self.assertEqual(value.version, "v1")
        self.assertFalse(value.truncated)
        self.assertIn('SUM("amount")', value.sql)

        windowed = assembler.metric_value(
            revenue_metric(name="jan_first"), window=("2025-01-01", "2025-01-01")
        )
        # Only web order 1 (amount 10.0) falls in that day; the app rows are in
        # February, so the window is a real filter rather than a coincidence.
        self.assertEqual(windowed.value, 10.0)
        self.assertEqual(windowed.grain, "scalar")

        by_channel = assembler.metric_by_dimension(
            revenue_metric(name="revenue"), dimension="channel"
        )
        self.assertEqual(by_channel.dimension, "channel")
        self.assertEqual(by_channel.grain, "categorical")
        # Ordered by value desc: app 1000 rows x 2.0 = 2000, web = 550.
        self.assertEqual(
            [(item["category"], item["value"]) for item in by_channel.buckets],
            [("app", 2000.0), ("web", 550.0)],
        )
        self.assertEqual(by_channel.bucket_sum, 2550.0)
        self.assertIn("GROUP BY", by_channel.sql)
        self.assertIn("ORDER BY 2 DESC", by_channel.sql)

        daily = assembler.metric_by_period(revenue_metric(name="revenue"), grain="daily")
        self.assertEqual(daily.grain, "daily")
        self.assertEqual(daily.points[0], {"period": "2025-01-01", "value": 10.0})
        self.assertEqual(
            [point["period"] for point in daily.points],
            sorted(point["period"] for point in daily.points),
        )
        self.assertEqual(daily.time_field, "order_date")
        # 10 January days (web) + 28 February days (app) are distinct buckets.
        self.assertEqual(daily.period_count, 10 + 28)

        monthly = assembler.metric_by_period(revenue_metric(name="revenue"), grain="monthly")
        self.assertEqual([point["period"] for point in monthly.points], ["2025-01", "2025-02"])
        self.assertEqual([point["value"] for point in monthly.points], [550.0, 2000.0])

    def test_truncated_input_is_marked_and_refused_for_trends(self) -> None:
        assembler = self.assembler(max_rows=1)
        truncated = assembler.metric_by_dimension(
            revenue_metric(name="revenue"), dimension="channel", allow_truncated=True
        )
        self.assertTrue(truncated.truncated)
        self.assertEqual(truncated.row_limit, 1)
        self.assertEqual(len(truncated.buckets), 1)

        with self.assertRaises(ValueError) as caught:
            assembler.metric_by_dimension(revenue_metric(name="revenue"), dimension="channel")
        self.assertIn("truncated_analysis_input", str(caught.exception))

        with self.assertRaises(ValueError) as series:
            assembler.metric_by_period(revenue_metric(name="revenue"), grain="daily")
        self.assertIn("truncated_analysis_input", str(series.exception))

        self.assertIs(assert_usable(truncated, purpose="x", allow_truncated=True), truncated)

    def test_metadata_guards_refuse_mixed_versions_and_units(self) -> None:
        assembler = self.assembler()
        jun = assembler.metric_value(revenue_metric(name="june"))
        other = assembler.metric_value(
            revenue_metric(name="cny_but_v2", version="v2", unit="CNY")
        )
        merged = assembler.combine_metadata(jun, assembler.metric_value(revenue_metric(name="x")))
        self.assertEqual(merged["unit"], "CNY")
        self.assertEqual(merged["version"], "v1")
        with self.assertRaises(ValueError):
            assembler.combine_metadata(jun, other)

        usd = assembler.metric_value(revenue_metric(name="usd", unit="USD"))
        with self.assertRaises(ValueError):
            assembler.combine_metadata(jun, usd)

    def test_unknown_tables_columns_and_aggregations_are_refused(self) -> None:
        assembler = self.assembler()
        with self.assertRaises(ValueError):
            assembler.metric_value(revenue_metric(entity_table="employees"))
        with self.assertRaises(ValueError):
            assembler.metric_by_dimension(revenue_metric(), dimension="nope")
        with self.assertRaises(ValueError):
            assembler.metric_value(revenue_metric(aggregation="median"))
        with self.assertRaises(ValueError):
            assembler.metric_by_period(revenue_metric(), grain="hourly")
        with self.assertRaises(ValueError):
            assembler.metric_value(revenue_metric(time_field=None), window=("2025-01-01", "2025-01-02"))
        with self.assertRaises(ValueError):
            assembler.metric_value(revenue_metric(filters={"channel": []}))
        with self.assertRaises(ValueError):
            assembler.metric_value(revenue_metric(filters={"nope": ["x"]}))

    def test_filters_are_governed_and_escaped(self) -> None:
        assembler = self.assembler()
        filtered = assembler.metric_value(
            revenue_metric(filters={"channel": ["web", "store"]}, name="web_and_store")
        )
        # web rows: 10+20+30+40+50+60+70+80+90+100 = 550.
        self.assertEqual(filtered.value, 550.0)
        self.assertIn("IN ('web', 'store')", filtered.sql)

        quoted = assembler.metric_value(
            revenue_metric(filters={"channel": ["web'; DROP TABLE orders --"]}, name="injection")
        )
        self.assertIsNone(quoted.value)
        self.assertIn("web''; DROP TABLE orders --", quoted.sql)
        # The escaped literal is data, not SQL: the table is still there.
        self.assertEqual(
            assembler.metric_value(revenue_metric(filters={"channel": ["web"]}, name="web")).value,
            550.0,
        )


if __name__ == "__main__":
    unittest.main()
